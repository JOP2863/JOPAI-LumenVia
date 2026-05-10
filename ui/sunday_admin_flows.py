"""Flux admin Dimanche : complément incrémental et régénération Vertex/GCS/Sheets."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path

import streamlit as st

from core.audio_utils import join_wav_bytes, normalize_audio_bytes
from core.gcp_clients import build_gcs_client
from core.gemini_tts_api import GeminiTtsApiClient
from core.pdf_liturgy_sunday import build_liturgy_sunday_pdf_bytes
from core.sheets_db import append_immutable_row, build_gspread_client
from core.storage import blob_exists, download_bytes, upload_bytes, upload_text
from core.local_bundle_cache import persist_sunday_bundle
from core.liturgy_theme import liturgical_accent_hex
from core.vertex_gemini import VertexGeminiClient
from core.voix_audio import pick_voice_name, resolve_voice
from core.gcs_signed_urls import gcs_signed_url
from ui.pages.about import _ABOUT_MARKDOWN


def _run_incremental_sunday_outputs(
    *,
    cfg: object,
    gs: object,
    gcs: object,
    identity: object,
    texts: object,
    zone: str,
    bundle_synth_text: str | None,
    bundle_audio_gcs_path: str | None,
    bundle_readings_gcs_path: str | None,
    include_catechese_pdf: bool,
    also_pdf_if_missing: bool,
    also_readings_if_missing: bool,
    pdf_key: str,
) -> None:
    """Sans nouvelle synthèse Vertex : audio des lectures (TTS) et/ou fascicule PDF si absents sur Cloud."""
    import app as ap
    date_str = str(identity.date)
    gen_row = ap._latest_generation_row_for_sunday(gs=gs, cfg=cfg, date_str=date_str, zone=zone)
    if not gen_row:
        st.error(
            "Aucune synthèse enregistrée pour cette date. Utilise d’abord « Tout régénérer (long) »."
        )
        return
    gen_eid = str(gen_row.get("entity_id") or "").strip()
    if not gen_eid:
        st.error("Enregistrement de génération invalide (identifiant manquant).")
        return

    synth = (bundle_synth_text or "").strip()
    if not synth:
        tp = str(gen_row.get("text_gcs_path") or "").strip()
        if tp:
            try:
                synth = (
                    download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=tp)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            except Exception as ex:
                st.warning(f"Lecture du texte de synthèse sur Cloud impossible : {ex}")
                synth = ""

    done: list[str] = []
    readings_path_this_run: str | None = None

    if (
        also_readings_if_missing
        and not ap._has_readings_audio_for_gen(gs=gs, cfg=cfg, gen_entity_id=gen_eid)
    ):
        readings_plain = ap._plain_readings_for_tts(texts)
        if not readings_plain.strip():
            st.warning("Texte des lectures vide — impossible de produire l’audio des lectures.")
        else:
            try:
                with st.spinner("Audio des lectures (TTS)…"):
                    templates_ia: dict[str, str] = {}
                    voix_r: list[dict] = []
                    try:
                        templates_ia = ap._load_prompt_templates_cached(
                            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
                            service_account_fingerprint=ap._service_account_fingerprint(
                                getattr(cfg, "gcp_service_account", {}) or {}
                            ),
                        )
                        voix_r = ap._load_voix_rules_cached(
                            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
                            service_account_fingerprint=ap._service_account_fingerprint(
                                getattr(cfg, "gcp_service_account", {}) or {}
                            ),
                        )
                    except Exception:
                        templates_ia = {}
                        voix_r = []
                    voice_read = pick_voice_name(
                        voix_r,
                        cible="lectures",
                        couleur=getattr(identity, "couleur", None),
                        periode=getattr(identity, "periode", None),
                    )
                    readings_tts = ap._compose_readings_tts_text(body=readings_plain, templates=templates_ia)
                    r_bytes, r_mime, r_ext = ap._tts_gemini_chunked_bytes(
                        cfg=cfg, text=readings_tts, voice_name=voice_read
                    )
                day_for_path_inc = str(getattr(identity, "date", "") or "").strip()[:10]
                readings_path = f"AudioLectures/{day_for_path_inc}/{gen_eid}.{r_ext}"
                upload_bytes(
                    gcs=gcs,
                    bucket_name=cfg.gcs_bucket_name,
                    path=readings_path,
                    data=r_bytes,
                    content_type=r_mime,
                )
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="audio",
                    values_by_col={
                        "entity_id": sha256(f"audio_lect|{gen_eid}|{readings_path}".encode("utf-8")).hexdigest()[:24],
                        "gen_entity_id": gen_eid,
                        "voice": voice_read,
                        "format": r_ext,
                        "gcs_path": readings_path,
                    },
                )
                readings_path_this_run = readings_path
                done.append("audio des lectures")
            except Exception as ex:
                st.warning(f"Audio des lectures non publié : {ex}")

    fasc_path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    need_pdf = bool(
        also_pdf_if_missing
        and synth
        and bucket
        and not blob_exists(gcs=gcs, bucket_name=bucket, path=fasc_path)
    )
    if need_pdf:
        try:
            with st.spinner("Fascicule PDF sur Cloud…"):
                img_b = ap._fetch_liturgy_illustration_full_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
                aud_url, aud_note = ap._public_app_listen_url(date_str=date_str)
                p_aud = (bundle_audio_gcs_path or "").strip() or ap._synthesis_audio_gcs_path_for_gen(
                    gs=gs, cfg=cfg, gen_entity_id=gen_eid
                )
                if p_aud:
                    signed = gcs_signed_url(gcs=gcs, bucket_name=bucket, path=p_aud)
                    if signed:
                        aud_url = signed
                synth_for_pdf = synth
                if not include_catechese_pdf:
                    synth_for_pdf = ap._strip_catechese_bridge(synth_for_pdf)
                back_cover_b = None
                try:
                    y = str(date_str)[:4]
                    back_cover_b = download_bytes(
                        gcs=gcs,
                        bucket_name=bucket,
                        path=f"Images/thumbs/montage_{y}.png",
                    )
                except Exception:
                    back_cover_b = None
                semaine_psautier = (getattr(identity, "semaine", None) or "").strip()
                line1 = ap._liturgy_display_label(
                    (getattr(identity, "fete", None) or "").strip()
                    or (ap._jour_liturgique(identity) or "").strip()
                    or ap._liturgy_cover_pdf_title(identity)
                )
                line2 = ""
                if semaine_psautier and ("psautier" in semaine_psautier.lower()):
                    lbl = ap._liturgy_display_label(semaine_psautier).strip()
                    line2 = f"({lbl})" if lbl else ""
                week_title_pdf = (line1 + ("\n" + line2 if line2 else "")).strip()
                highlight_idx = None
                try:
                    manifest = json.loads(
                        Path("data/manifests/illustration_pipeline.json").read_text(encoding="utf-8")
                    )
                    targets = manifest.get("targets") or []
                    year = str(date_str)[:4]
                    year_targets = [t for t in targets if str(t.get("date") or "").startswith(year)]
                    year_dates = [str(t.get("date") or "")[:10] for t in year_targets]
                    if str(date_str)[:10] in year_dates:
                        highlight_idx = int(year_dates.index(str(date_str)[:10]))
                except Exception:
                    highlight_idx = None
                rp_for_cover = (readings_path_this_run or "").strip() or (
                    (bundle_readings_gcs_path or "").strip()
                )
                readings_pdf_signed = None
                if rp_for_cover:
                    try:
                        readings_pdf_signed = gcs_signed_url(
                            gcs=gcs, bucket_name=bucket, path=rp_for_cover
                        ) or None
                    except Exception:
                        readings_pdf_signed = None
                pdf_b = build_liturgy_sunday_pdf_bytes(
                    image_bytes=img_b,
                    week_title=week_title_pdf,
                    date_line=ap._french_long_date_label(date_str),
                    meta_line=(
                        f"{ap._liturgy_display_label(getattr(identity, 'periode', None))} · "
                        f"Cycle {ap._cycle_year_display(getattr(identity, 'annee', None))} · "
                        f"{ap._liturgy_display_label(getattr(identity, 'couleur', None))}"
                    ),
                    premiere_lecture=texts.premiere_lecture,
                    psaume=texts.psaume,
                    deuxieme_lecture=texts.deuxieme_lecture,
                    evangile=texts.evangile,
                    synthesis_text=synth_for_pdf,
                    audio_listen_url=aud_url,
                    audio_listen_note=aud_note,
                    audio_readings_listen_url=readings_pdf_signed,
                    about_markdown=_ABOUT_MARKDOWN,
                    back_cover_image_bytes=back_cover_b,
                    accent_hex=liturgical_accent_hex(getattr(identity, "couleur", None)),
                    back_cover_highlight_cell_index=highlight_idx,
                )
                upload_bytes(
                    gcs=gcs,
                    bucket_name=bucket,
                    path=fasc_path,
                    data=pdf_b,
                    content_type="application/pdf",
                )
                st.session_state[pdf_key] = pdf_b
                done.append("fascicule PDF")
        except Exception as ex:
            st.warning(f"Fascicule PDF non produit : {ex}")

    if not done:
        st.info(
            "Rien à compléter : l’audio des lectures et le fascicule PDF sont déjà présents "
            "(selon les cases et le stockage Cloud)."
        )
    else:
        st.success("Complété : " + " · ".join(done) + ".")


def _run_generate_sunday_flow(
    *,
    _overlay: object,
    identity: object,
    texts: object,
    zone: str,
    total_words: int,
    pct: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    generate_pdf: bool,
    generate_readings_audio: bool,
    debug: bool,
    cfg: object,
) -> None:
    import app as ap
    target_words = max(80, int(total_words * (pct / 100.0)))
    # La Passerelle catéchèse ajoute un module structuré : on augmente le budget pour éviter qu’elle disparaisse.
    if include_catechese_bridge:
        target_words += 180
    templates: dict[str, str] = {}
    try:
        templates = ap._load_prompt_templates_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=ap._service_account_fingerprint(getattr(cfg, "gcp_service_account", {}) or {}),
        )
    except Exception:
        templates = {}

    voix_rows: list[dict] = []
    try:
        voix_rows = ap._load_voix_rules_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=ap._service_account_fingerprint(getattr(cfg, "gcp_service_account", {}) or {}),
        )
    except Exception:
        voix_rows = []

    instructions_struct = templates.get("instructions_base_md") or Path("data/instructions_ia.md").read_text(
        encoding="utf-8"
    )
    # Double blind : la "secret sauce" n'est pas dans Sheets (A), mais dans st.secrets (B).
    try:
        s = st.secrets
        secret_sauce = str(s.get("IA_SECRET_SAUCE_MD") or s.get("ia_secret_sauce_md") or "").strip()
    except Exception:
        secret_sauce = ""
    instructions = (instructions_struct + "\n\n" + secret_sauce).strip() if secret_sauce else instructions_struct
    liturgical_context = "\n".join(
        [
            f"- Temps liturgique ({identity.periode or '—'}): {ap._explain_liturgical_time(identity.periode)}",
            f"- Couleur ({identity.couleur or '—'}): {ap._explain_liturgical_color(identity.couleur)}",
            f"- Année / cycle ({identity.annee or '—'}): {ap._explain_liturgical_cycle(identity.annee)}",
        ]
    )
    prompt = ap._build_prompt(
        instructions=instructions,
        length_words=int(target_words),
        include_takeaways=bool(include_takeaways),
        include_catechese_bridge=bool(include_catechese_bridge),
        templates=templates,
        identity={
            "date": identity.date,
            "zone": identity.zone,
            "periode": identity.periode,
            "annee": identity.annee,
            "couleur": identity.couleur,
            "fete": identity.fete,
            "jour_liturgique_nom": ap._jour_liturgique(identity),
        },
        readings={
            "premiere_lecture": texts.premiere_lecture,
            "psaume": texts.psaume,
            "deuxieme_lecture": texts.deuxieme_lecture,
            "evangile": texts.evangile,
        },
        liturgical_context=liturgical_context,
    )

    source_hash = sha256(
        (identity.date + "|" + (texts.premiere_lecture or "") + "|" + (texts.psaume or "") + "|" + (texts.evangile or "")).encode(
            "utf-8"
        )
    ).hexdigest()

    vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
    perf: dict[str, float | int | str] = {}
    with st.spinner("Génération IA (Gemini)…"):
        t0 = time.perf_counter()
        try:
            # Évite les synthèses tronquées : 2048 tokens est souvent trop court pour une synthèse “longue”.
            # Heuristique simple (français) : ~2.2 tokens / mot avec marge.
            max_out = min(8192, max(2048, int(target_words * 2.2)))
            gen = vx.generate_text_auto(
                preferred_models=[
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-pro-latest",
                    "gemini-flash-latest",
                ],
                prompt=prompt,
                max_output_tokens=max_out,
            )
        except Exception as e:
            if debug:
                st.exception(e)
            else:
                st.error("Erreur lors de la génération de la synthèse. Active le mode debug pour détails.")
            return
        t1 = time.perf_counter()
        perf["vertex_text_s"] = round(t1 - t0, 3)

    # Fiabilisation : si la sortie est tronquée, on retente automatiquement une fois
    # avec un modèle moins “pensant” et un budget de sortie maximal.
    cand0 = ((gen.raw or {}).get("candidates") or [{}])[0]
    fr = str(cand0.get("finishReason") or "").strip().upper()
    words_out = len((gen.text or "").split())
    has_citations = bool((cand0.get("citationMetadata") or {}).get("citations")) if isinstance(cand0, dict) else False
    looks_truncated = (fr in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH")) or (words_out < int(target_words * 0.85))
    if looks_truncated or has_citations:
        # Prompt “durci” : aucune URL / aucune citation / uniquement textes fournis.
        hardened_prefix = templates.get("retry_hardened_prefix") or (
            "IMPORTANT — SOURCES: ne cite aucune source externe, aucune URL, aucun site web. "
            "Utilise exclusivement les textes AELF fournis ci-dessous. "
            "IMPORTANT — FORMAT: réponds uniquement avec la synthèse, sans préambule technique."
        )
        hardened = hardened_prefix.strip() + "\n\n" + prompt
        try:
            t0b = time.perf_counter()
            gen2 = vx.generate_text_auto(
                preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
                prompt=hardened,
                max_output_tokens=8192,
            )
            perf["vertex_text_retry_s"] = round(time.perf_counter() - t0b, 3)
            cand0b = ((gen2.raw or {}).get("candidates") or [{}])[0]
            fr2 = str(cand0b.get("finishReason") or "").strip().upper()
            words2 = len((gen2.text or "").split())
            cites2 = bool((cand0b.get("citationMetadata") or {}).get("citations")) if isinstance(cand0b, dict) else False
            if (fr2 in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH")) or (words2 < int(target_words * 0.85)) or cites2:
                st.error(
                    "Synthèse incomplète malgré une relance automatique (MAX_TOKENS ou contenu trop court / citations). "
                    "Réessaie plus tard, ou réduis le % demandé."
                )
                if debug:
                    st.write(
                        {
                            "finishReason_1": fr,
                            "words_1": words_out,
                            "finishReason_2": fr2,
                            "words_2": words2,
                            "has_citations_1": has_citations,
                            "has_citations_2": cites2,
                        }
                    )
                return
            gen = gen2
        except Exception as e:
            if debug:
                st.exception(e)
            else:
                st.error("Relance automatique impossible (quota/erreur). Réessaie dans quelques minutes.")
            return

    if debug:
        usage = (gen.raw or {}).get("usageMetadata") or {}
        cand0 = ((gen.raw or {}).get("candidates") or [{}])[0]
        st.markdown("**Debug génération**")
        st.write(
            {
                "model": gen.model,
                "elapsed_s": perf.get("vertex_text_s"),
                "finishReason": cand0.get("finishReason"),
                "promptTokenCount": usage.get("promptTokenCount"),
                "candidatesTokenCount": usage.get("candidatesTokenCount"),
                "totalTokenCount": usage.get("totalTokenCount"),
                "text_chars": len(gen.text or ""),
                "text_words": len((gen.text or "").split()),
                "target_words": int(target_words),
                "maxOutputTokens": int(max_out),
            }
        )
        with st.expander("Prompt envoyé à Gemini (debug)", expanded=False):
            st.text_area("Prompt complet", value=prompt, height=320)
        with st.expander("Réponse brute Vertex (debug)", expanded=False):
            st.write(gen.raw)
        if str(cand0.get("finishReason") or "").strip().upper() in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH"):
            st.warning(
                "La synthèse semble tronquée (finishReason = MAX_TOKENS). "
                "Augmenter encore `maxOutputTokens` ou réduire le % demandé."
            )

    if not gen.text.strip():
        st.error("Réponse IA vide.")
        return

    gcs = build_gcs_client(cfg.gcp_service_account)
    gs = build_gspread_client(cfg.gcp_service_account)

    gen_entity_id = sha256(f"{identity.date}|{zone}|{source_hash}".encode("utf-8")).hexdigest()[:24]

    text_path = f"Syntheses/{identity.date}/{gen_entity_id}.txt"
    ut0 = time.perf_counter()
    upload_text(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path, text=gen.text)
    perf["upload_text_s"] = round(time.perf_counter() - ut0, 3)

    row_gen = append_immutable_row(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table="generations",
        values_by_col={
            "entity_id": gen_entity_id,
            "date": identity.date,
            "zone": zone,
            "cycle": identity.annee or "",
            "season": identity.periode or "",
            "length": int(target_words),
            "prompt_version": "v1",
            "model": gen.model,
            "source_hash": source_hash,
            "text_gcs_path": text_path,
        },
    )

    voice_syn_res = resolve_voice(
        voix_rows,
        cible="synthese",
        couleur=getattr(identity, "couleur", None),
        periode=getattr(identity, "periode", None),
    )
    voice_syn = str(voice_syn_res["voice"])
    perf["voice_synthese"] = voice_syn
    perf["voice_synthese_rule_id"] = ((voice_syn_res.get("rule") or {}).get("#ID") or "")
    perf["voice_synthese_fallback"] = bool(voice_syn_res.get("fallback"))
    if debug:
        if voice_syn_res.get("fallback"):
            st.warning(
                f"Aucune règle Voix_Audio ne matche cette synthèse — fallback voix par défaut **{voice_syn}**."
            )
        else:
            st.caption(
                f"Voix synthèse retenue : **{voice_syn}** "
                f"(règle `#ID {perf['voice_synthese_rule_id']}`, score {voice_syn_res.get('score')})."
            )
    tts_payload = ap._compose_synthesis_tts_text(
        body=gen.text or "",
        templates=templates,
        periode=getattr(identity, "periode", None),
    )

    audio_route = "vertex"
    with st.spinner("Synthèse audio (Vertex AI)…"):
        try:
            at0 = time.perf_counter()
            audio = vx.generate_audio_auto(
                preferred_models=[
                    "gemini-2.5-flash-preview-tts",
                    "gemini-2.5-pro-preview-tts",
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                ],
                text=tts_payload,
                voice_name=voice_syn,
            )
            perf["audio_vertex_s"] = round(time.perf_counter() - at0, 3)
        except Exception as e:
            # Fallback si Vertex refuse AUDIO (allowlist) OU si erreur transitoire/quota.
            msg = str(e).lower()
            allowlist = ("not allowlisted" in msg) or ("allowlisted" in msg)
            transient = ("429" in msg) or ("quota" in msg) or ("rate" in msg) or ("tempor" in msg) or ("503" in msg)
            if (allowlist or transient) and cfg.gemini_api_key:
                audio_route = "gemini_api_chunked"
                ft0 = time.perf_counter()
                tts_api = GeminiTtsApiClient(api_key=cfg.gemini_api_key)
                chunks = ap._chunk_text_for_tts(tts_payload, max_chars=1400)
                perf["tts_chunks"] = len(chunks)
                wav_parts_by_i: dict[int, bytes] = {}
                tts_chunk_total_s = 0.0
                tts_errors: list[str] = []

                def _tts_job(i: int, ch: str) -> tuple[int, bytes, float]:
                    ct0 = time.perf_counter()
                    tts_audio = tts_api.generate_audio(
                        model="gemini-2.5-flash-preview-tts",
                        text=ch,
                        voice_name=voice_syn,
                    )
                    elapsed = time.perf_counter() - ct0
                    b, mt, _ = normalize_audio_bytes(audio_bytes=tts_audio.audio_bytes, mime_type=tts_audio.mime_type)
                    if mt != "audio/wav":
                        b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
                    return i, b, elapsed

                # Quotas Gemini API : réduire le parallélisme limite les 429.
                workers = max(1, min(2, len(chunks)))
                with ThreadPoolExecutor(max_workers=workers) as ex2:
                    futs = [ex2.submit(_tts_job, i, ch) for i, ch in enumerate(chunks)]
                    for fut in as_completed(futs):
                        try:
                            i, b, elapsed = fut.result()
                            wav_parts_by_i[i] = b
                            tts_chunk_total_s += float(elapsed)
                        except Exception as ex:
                            tts_errors.append(str(ex))

                if tts_errors or len(wav_parts_by_i) != len(chunks):
                    st.error(
                        "Audio incomplet : certains morceaux TTS ont échoué (quota/erreur). "
                        "Réessaie dans quelques minutes."
                    )
                    if debug and tts_errors:
                        st.write({"tts_errors": tts_errors[:6], "chunks_ok": len(wav_parts_by_i), "chunks_total": len(chunks)})
                    st.stop()

                wav_parts = [wav_parts_by_i[i] for i in range(len(chunks)) if i in wav_parts_by_i]
                joined = join_wav_bytes(wav_parts)
                perf["audio_fallback_s"] = round(time.perf_counter() - ft0, 3)
                perf["tts_chunk_total_s"] = round(tts_chunk_total_s, 3)
                audio = type("AudioWrap", (), {})()
                audio.audio_bytes = joined
                audio.mime_type = "audio/wav"
                audio.model = "gemini-api-tts:chunked"
            else:
                # Pas de clé Gemini : on remonte l'erreur.
                if allowlist and not cfg.gemini_api_key:
                    st.error(
                        "Audio indisponible via Vertex AI (compte non allowlist AUDIO). "
                        "Ajoute/valide GEMINI_API_KEY pour activer le fallback TTS."
                    )
                    st.stop()
                raise

        if not getattr(audio, "audio_bytes", b""):
            st.error("Réponse audio vide.")
            st.stop()

    audio_bytes_norm, audio_mime_norm, audio_ext = normalize_audio_bytes(
        audio_bytes=getattr(audio, "audio_bytes", b""),
        mime_type=getattr(audio, "mime_type", None),
    )
    audio_path = f"Audio/{identity.date}/{gen_entity_id}.{audio_ext}"
    uat0 = time.perf_counter()
    upload_bytes(
        gcs=gcs,
        bucket_name=cfg.gcs_bucket_name,
        path=audio_path,
        data=audio_bytes_norm,
        content_type=audio_mime_norm,
    )
    perf["upload_audio_s"] = round(time.perf_counter() - uat0, 3)
    perf["audio_route"] = audio_route

    append_immutable_row(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table="audio",
        values_by_col={
            "entity_id": sha256(f"audio|{gen_entity_id}|{audio_path}".encode("utf-8")).hexdigest()[:24],
            "gen_entity_id": row_gen["entity_id"],
            "voice": voice_syn,
            "format": audio_ext,
            "gcs_path": audio_path,
        },
    )

    persist_sunday_bundle(
        date_str=str(identity.date),
        zone=zone,
        synth_text=gen.text,
        audio_bytes=audio_bytes_norm,
        audio_mime=audio_mime_norm,
    )

    readings_cover_signed: str | None = None
    if generate_readings_audio:
        readings_plain = ap._plain_readings_for_tts(texts)
        if readings_plain.strip():
            try:
                with st.spinner("LumenVia génère l’audio des lectures (AELF)…"):
                    voice_read_res = resolve_voice(
                        voix_rows,
                        cible="lectures",
                        couleur=getattr(identity, "couleur", None),
                        periode=getattr(identity, "periode", None),
                    )
                    voice_read = str(voice_read_res["voice"])
                    perf["voice_lectures"] = voice_read
                    perf["voice_lectures_rule_id"] = ((voice_read_res.get("rule") or {}).get("#ID") or "")
                    perf["voice_lectures_fallback"] = bool(voice_read_res.get("fallback"))
                    if debug:
                        if voice_read_res.get("fallback"):
                            st.warning(
                                f"Aucune règle Voix_Audio (lectures) ne matche — fallback **{voice_read}**."
                            )
                        else:
                            st.caption(
                                f"Voix lectures retenue : **{voice_read}** "
                                f"(règle `#ID {perf['voice_lectures_rule_id']}`)."
                            )
                    readings_tts = ap._compose_readings_tts_text(body=readings_plain, templates=templates)
                    r_bytes, r_mime, r_ext = ap._tts_gemini_chunked_bytes(
                        cfg=cfg, text=readings_tts, voice_name=voice_read
                    )
                day_for_path = str(getattr(identity, "date", "") or "").strip()[:10]
                readings_path = f"AudioLectures/{day_for_path}/{gen_entity_id}.{r_ext}"
                upload_bytes(
                    gcs=gcs,
                    bucket_name=cfg.gcs_bucket_name,
                    path=readings_path,
                    data=r_bytes,
                    content_type=r_mime,
                )
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="audio",
                    values_by_col={
                        "entity_id": sha256(f"audio_lect|{gen_entity_id}|{readings_path}".encode("utf-8")).hexdigest()[
                            :24
                        ],
                        "gen_entity_id": row_gen["entity_id"],
                        "voice": voice_read,
                        "format": r_ext,
                        "gcs_path": readings_path,
                    },
                )
                try:
                    readings_cover_signed = (
                        gcs_signed_url(
                            gcs=gcs,
                            bucket_name=str(cfg.gcs_bucket_name).strip(),
                            path=readings_path,
                        )
                        or None
                    )
                except Exception:
                    readings_cover_signed = None
            except Exception as ex:
                st.warning(f"Audio des lectures non publié (synthèse enregistrée quand même) : {ex}")
        else:
            st.warning(
                "Audio des lectures ignoré : le texte agrégé des quatre lectures (AELF) est vide — "
                "vérifie les lectures pour cette date (cache / API)."
            )

    # Optimisation : les downloads de vérification (Cloud → UI) sont coûteux.
    # On ne les fait que si debug est activé.
    if debug:
        st.subheader("Résumé du temps liturgique")
        try:
            dt0 = time.perf_counter()
            txt_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path)
            txt = txt_bytes.decode("utf-8", errors="replace")
            perf["download_text_verify_s"] = round(time.perf_counter() - dt0, 3)
        except Exception as e:
            txt = f"[Erreur lecture Cloud texte] {e}"
        st.text_area("Synthèse", value=txt, height=320)

        try:
            da0 = time.perf_counter()
            aud_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=audio_path)
            aud_play, aud_mime_play, _ = normalize_audio_bytes(audio_bytes=aud_bytes, mime_type=audio_mime_norm)
            perf["download_audio_verify_s"] = round(time.perf_counter() - da0, 3)
            st.subheader("Écouter le résumé")
            st.audio(aud_play, format=aud_mime_play)
        except Exception as e:
            st.error(f"Erreur lecture/lecture audio Cloud: {e}")

    # Fascicule PDF : toujours en dernier (après texte Vertex, audio synthèse, audio lectures) pour que la couverture
    # puisse réutiliser les URLs signées des pistes déjà uploadées sur GCS.
    if generate_pdf and cfg.gcs_bucket_name:
        if generate_readings_audio and not readings_cover_signed:
            st.info(
                "Le PDF sera généré **sans** lien « Écouter les lectures » sur la couverture : "
                "l’audio des lectures n’a pas été produit ou signé dans cette passe.",
                icon="ℹ️",
            )
        try:
            tpdf0 = time.perf_counter()
            date_str = str(identity.date)
            img_b = ap._fetch_liturgy_illustration_full_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
            back_cover_b = None
            try:
                y = str(date_str)[:4]
                back_cover_b = download_bytes(
                    gcs=gcs,
                    bucket_name=str(cfg.gcs_bucket_name).strip(),
                    path=f"Images/thumbs/montage_{y}.png",
                )
            except Exception:
                back_cover_b = None

            # Titre 2 lignes comme dans “Préparer le PDF…” (Psautier uniquement)
            semaine_psautier = (getattr(identity, "semaine", None) or "").strip()
            line1 = ap._liturgy_display_label(
                (getattr(identity, "fete", None) or "").strip()
                or (ap._jour_liturgique(identity) or "").strip()
                or ap._liturgy_cover_pdf_title(identity)
            )
            line2 = ""
            if semaine_psautier and ("psautier" in semaine_psautier.lower()):
                lbl = ap._liturgy_display_label(semaine_psautier).strip()
                line2 = f"({lbl})" if lbl else ""
            week_title_pdf = (line1 + ("\n" + line2 if line2 else "")).strip()

            # highlight index (best-effort)
            highlight_idx = None
            try:
                manifest = json.loads(
                    Path("data/manifests/illustration_pipeline.json").read_text(encoding="utf-8")
                )
                targets = manifest.get("targets") or []
                year = str(date_str)[:4]
                year_targets = [t for t in targets if str(t.get("date") or "").startswith(year)]
                year_dates = [str(t.get("date") or "")[:10] for t in year_targets]
                if str(date_str)[:10] in year_dates:
                    highlight_idx = int(year_dates.index(str(date_str)[:10]))
            except Exception:
                highlight_idx = None

            aud_url, aud_note = ap._public_app_listen_url(date_str=date_str)
            pdf_b = build_liturgy_sunday_pdf_bytes(
                image_bytes=img_b,
                week_title=week_title_pdf,
                date_line=ap._french_long_date_label(date_str),
                meta_line=(
                    f"{ap._liturgy_display_label(getattr(identity, 'periode', None))} · "
                    f"Cycle {ap._cycle_year_display(getattr(identity, 'annee', None))} · "
                    f"{ap._liturgy_display_label(getattr(identity, 'couleur', None))}"
                ),
                premiere_lecture=getattr(texts, "premiere_lecture", None),
                psaume=getattr(texts, "psaume", None),
                deuxieme_lecture=getattr(texts, "deuxieme_lecture", None),
                evangile=getattr(texts, "evangile", None),
                synthesis_text=gen.text,
                audio_listen_url=aud_url,
                audio_listen_note=aud_note,
                audio_readings_listen_url=readings_cover_signed,
                about_markdown=_ABOUT_MARKDOWN,
                back_cover_image_bytes=back_cover_b,
                accent_hex=liturgical_accent_hex(getattr(identity, "couleur", None)),
                back_cover_highlight_cell_index=highlight_idx,
            )
            fasc_path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
            upload_bytes(
                gcs=gcs,
                bucket_name=str(cfg.gcs_bucket_name).strip(),
                path=fasc_path,
                data=pdf_b,
                content_type="application/pdf",
            )
            st.session_state[f"liturgy_sunday_pdf_{date_str}"] = pdf_b
            perf["pdf_auto_s"] = round(time.perf_counter() - tpdf0, 3)
        except Exception as e:
            st.warning(f"PDF non généré automatiquement : {e}")
    if debug:
        total_keys = (
            "vertex_text_s",
            "upload_text_s",
            "audio_vertex_s",
            "audio_fallback_s",
            "tts_chunk_total_s",
            "upload_audio_s",
            "download_text_verify_s",
            "download_audio_verify_s",
        )
        perf["perf_total_tracked_s"] = round(
            sum(float(perf.get(k) or 0) for k in total_keys if isinstance(perf.get(k), (int, float))),
            3,
        )
        st.markdown("**Chronométrage (debug)**")
        st.write(perf)
