#!/usr/bin/env python3
"""Vigila el API publico de RoomMatch y avisa cuando aparece un anuncio nuevo.

Uso:
    python monitor.py                  una pasada (lo normal desde el Programador de tareas)
    python monitor.py --dry-run        muestra lo nuevo sin guardar estado ni notificar
    python monitor.py --test-notify    envia un mensaje de prueba a los canales configurados
    python monitor.py --notify-existing  en la primera ejecucion notifica tambien lo ya publicado
    python monitor.py --state ruta.json  usa otro fichero de estado

La configuracion se lee de config.json (ver config.example.json). Las variables de
entorno SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO,
TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID tienen prioridad.
"""

import argparse
import json
import logging
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
API_URL = (
    "https://roommatching-aanbodapi.zig365.nl/api/v1/actueel-aanbod"
    "?limit=100&locale=en_GB&page=0&sort=%2BreactionData.aangepasteTotaleHuurprijs"
)
WEB_URL = "https://www.roommatch.nl/en/offerings/to-rent/details"
REQUEST_BODY = {
    "filters": {
        "$and": [
            {
                "$or": [
                    {"$and": [{"regio.id": {"$eq": "1"}}]},
                    {"$and": [{"regio.id": {"$eq": "3"}}]},
                ]
            },
            {"model.modelCategorie.id": {"$eq": "3"}},
            {
                "$or": [
                    {
                        "$and": [
                            {"reactionData.aangepasteNettoHuurprijs": {"$gte": "0"}},
                            {"reactionData.aangepasteNettoHuurprijs": {}},
                        ]
                    },
                    {
                        "$or": [
                            {
                                "$and": [
                                    {"totaleHuurVan": {"$gte": "0"}},
                                    {"totaleHuurVan": {}},
                                ]
                            },
                            {
                                "$and": [
                                    {"totaleHuurTot": {"$gte": "0"}},
                                    {"totaleHuurTot": {}},
                                ]
                            },
                        ]
                    },
                ]
            },
        ]
    },
    "hidden-filters": {
        "$and": [
            {"dwellingType.categorie": {"$eq": "woning"}},
            {"rentBuy": {"$eq": "Huur"}},
            {"isExtraAanbod": {"$eq": ""}},
            {"isWoningruil": {"$eq": ""}},
            {
                "$and": [
                    {
                        "$or": [
                            {"street": {"$like": ""}},
                            {"houseNumber": {"$like": ""}},
                            {"houseNumberAddition": {"$like": ""}},
                        ]
                    },
                    {
                        "$or": [
                            {"street": {"$like": ""}},
                            {"houseNumber": {"$like": ""}},
                            {"houseNumberAddition": {"$like": ""}},
                        ]
                    },
                ]
            },
        ]
    },
}
REQUEST_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-GB,en;q=0.9",
    "content-type": "application/json; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
    "Referer": "https://www.roommatch.nl/en/offerings/to-rent",
    "Origin": "https://www.roommatch.nl",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}

log = logging.getLogger("roommatch")


def load_config():
    path = BASE_DIR / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    smtp = cfg.setdefault("smtp", {})
    telegram = cfg.setdefault("telegram", {})
    filters = cfg.setdefault("filters", {})
    env = os.environ
    for key, name in (
        ("host", "SMTP_HOST"),
        ("port", "SMTP_PORT"),
        ("user", "SMTP_USER"),
        ("password", "SMTP_PASSWORD"),
        ("from", "MAIL_FROM"),
    ):
        if env.get(name):
            smtp[key] = env[name]
    if env.get("MAIL_TO"):
        smtp["to"] = [x.strip() for x in env["MAIL_TO"].split(",") if x.strip()]
    if env.get("TELEGRAM_BOT_TOKEN"):
        telegram["bot_token"] = env["TELEGRAM_BOT_TOKEN"]
    if env.get("TELEGRAM_CHAT_ID"):
        telegram["chat_id"] = env["TELEGRAM_CHAT_ID"]
    if env.get("FILTERS_JSON"):
        try:
            filters.update(json.loads(env["FILTERS_JSON"]))
        except json.JSONDecodeError:
            log.warning("FILTERS_JSON no contiene JSON valido, se ignora")
    if smtp.get("port"):
        smtp["port"] = int(smtp["port"])
    if isinstance(smtp.get("to"), str):
        smtp["to"] = [x.strip() for x in smtp["to"].split(",") if x.strip()]
    return cfg


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        BASE_DIR / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        log.addHandler(stream)


def fetch_listings():
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(REQUEST_BODY).encode("utf-8"),
        method="POST",
        headers=REQUEST_HEADERS,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload.get("data", [])


def listing_id(item):
    return str(item.get("id") or item.get("ID"))


def listing_url(item):
    key = item.get("urlKey")
    if key:
        return f"{WEB_URL}/{key}"
    return f"{WEB_URL}?dwellingID={listing_id(item)}"


def money(value):
    if value is None:
        return "?"
    return f"EUR {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
            "%d-%m-%Y"
        )
    except ValueError:
        return str(value)[:10]


def matches_filters(item, filters):
    if filters.get("max_total_rent") is not None:
        rent = item.get("totalRent")
        if rent is None or rent > filters["max_total_rent"]:
            return False
    if filters.get("min_area") is not None:
        area = item.get("areaDwelling")
        if area is None or area < filters["min_area"]:
            return False
    cities = [c.lower() for c in filters.get("cities") or []]
    if cities:
        city = ((item.get("city") or {}).get("name") or "").lower()
        if city not in cities:
            return False
    return True


def describe(item):
    address = " ".join(
        str(x)
        for x in (
            item.get("street"),
            item.get("houseNumber"),
            item.get("houseNumberAddition"),
        )
        if x
    ).strip()
    city = (item.get("city") or {}).get("name") or item.get("gemeenteGeoLocatieNaam") or ""
    reaction = item.get("reactionData") or {}
    rent = reaction.get("aangepasteTotaleHuurprijs") or item.get("totalRent")
    net = reaction.get("aangepasteNettoHuurprijs") or item.get("netRent")
    lines = [
        f"{address}, {city}",
        f"Alquiler total: {money(rent)}  |  Neto: {money(net)}",
        f"Superficie: {item.get('areaDwelling') or '?'} m2  |  "
        f"Tipo: {(item.get('dwellingType') or {}).get('localizedName') or '?'}",
        f"Publicado: {date(item.get('publicationDate')) or '?'}",
        f"Respuestas hasta ahora: {item.get('numberOfReactions', '?')}",
        f"Reaccionar antes de: {date(item.get('closingDate')) or '?'}",
        f"Disponible desde: {date(item.get('availableFromOriginalDate')) or '?'}",
        f"Anuncio: {listing_url(item)}",
    ]
    short = item.get("infoveldKort")
    if short:
        lines.insert(4, f"Nota: {short}")
    return "\n".join(lines)


def build_message(items):
    if len(items) == 1:
        reactions = items[0].get("numberOfReactions")
        suffix = f" ({reactions} respuestas)" if reactions is not None else ""
        subject = (
            "[RoomMatch] Nuevo anuncio"
            + suffix
            + ": "
            + describe(items[0]).splitlines()[0]
        )
    else:
        subject = f"[RoomMatch] {len(items)} nuevos anuncios"
    text = "\n\n".join(f"{i + 1}. {describe(item)}" for i, item in enumerate(items))
    blocks = []
    for item in items:
        blocks.append(
            "<div style='margin:0 0 18px;padding:12px 14px;border:1px solid #ddd;"
            "border-radius:8px;font-family:Arial,sans-serif'>"
            "<pre style='white-space:pre-wrap;font-family:inherit;font-size:14px;"
            "margin:0'>"
            + describe(item).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</pre></div>"
        )
    html = (
        "<div style='font-family:Arial,sans-serif'>"
        f"<h2 style='margin:0 0 14px'>RoomMatch: {len(items)} nuevo(s)</h2>"
        + "".join(blocks)
        + "</div>"
    )
    return subject, text, html


def send_email(cfg, subject, text, html):
    smtp_cfg = cfg.get("smtp") or {}
    required = ("host", "user", "password", "to")
    if not all(smtp_cfg.get(key) for key in required):
        return False, "email no configurado"
    sender = smtp_cfg.get("from") or smtp_cfg["user"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(smtp_cfg["to"])
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port") or 465)
    if port == 465:
        with smtplib.SMTP_SSL(
            host, port, context=ssl.create_default_context(), timeout=30
        ) as server:
            server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(message)
    return True, "email enviado"


def send_telegram(cfg, text):
    telegram_cfg = cfg.get("telegram") or {}
    token = telegram_cfg.get("bot_token")
    chat_id = telegram_cfg.get("chat_id")
    if not token or not chat_id:
        return False, "telegram no configurado"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
    return True, "telegram enviado"


def notify(cfg, subject, text, html):
    results = []
    if (cfg.get("smtp") or {}).get("user"):
        try:
            results.append(send_email(cfg, subject, text, html))
        except Exception as exc:
            results.append((False, f"email fallo: {exc}"))
    if (cfg.get("telegram") or {}).get("bot_token"):
        try:
            results.append(send_telegram(cfg, f"{subject}\n\n{text}"))
        except Exception as exc:
            results.append((False, f"telegram fallo: {exc}"))
    for ok, detail in results:
        log.info(detail if ok else detail)
    return results


def load_state(path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("estado ilegible, se regenera")
        return None
    if not isinstance(data.get("seen"), dict):
        return None
    return data


def save_state(path, seen):
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "seen": seen,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temp, path)


def persist_state(path, seen, previous):
    if previous is not None and previous == seen:
        return
    save_state(path, seen)


def main():
    parser = argparse.ArgumentParser(description="Monitor de nuevos anuncios RoomMatch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-notify", action="store_true")
    parser.add_argument("--notify-existing", action="store_true")
    parser.add_argument("--state", type=Path, default=BASE_DIR / "state.json")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()

    if args.test_notify:
        subject = "[RoomMatch] Mensaje de prueba"
        text = "Si recibes esto, las notificaciones funcionan."
        results = notify(cfg, subject, text, f"<p>{text}</p>")
        if not results:
            log.error("no hay ningun canal configurado en config.json")
            return 1
        return 0 if any(ok for ok, _ in results) else 1

    try:
        items = fetch_listings()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.error("no se pudo consultar el API: %s", exc)
        return 1

    log.info("anuncios devueltos por el API: %d", len(items))
    fetched = {listing_id(item): item for item in items}
    now = datetime.now(timezone.utc).isoformat()

    state = load_state(args.state)
    if state is None:
        if args.dry_run:
            log.info("primera ejecucion: se notificarian %d anuncios existentes", len(items))
            return 0
        seen = {key: now for key in fetched}
        save_state(args.state, seen)
        if not args.notify_existing:
            log.info("primera ejecucion: %d anuncios guardados como vistos", len(fetched))
            return 0
        candidates = list(fetched.values())
    else:
        seen = state["seen"]
        candidates = [item for key, item in fetched.items() if key not in seen]

    filters = cfg.get("filters") or {}
    to_notify = [item for item in candidates if matches_filters(item, filters)]
    filtered_out = len(candidates) - len(to_notify)
    if filtered_out:
        log.info("%d anuncio(s) nuevo(s) descartado(s) por filtros", filtered_out)

    if not to_notify:
        log.info("sin novedades")
        if not args.dry_run:
            merged = dict(seen)
            merged.update({key: seen.get(key, now) for key in fetched})
            persist_state(args.state, merged, seen)
        return 0

    to_notify.sort(key=lambda item: str(item.get("publicationDate") or ""))
    subject, text, html = build_message(to_notify)

    if args.dry_run:
        log.info("DRY-RUN: se notificarian %d anuncio(s):", len(to_notify))
        print(text)
        return 0

    results = notify(cfg, subject, text, html)
    delivered = any(ok for ok, _ in results)
    if not results:
        log.error("hay anuncios nuevos pero no hay canales configurados; no se marcan como vistos")
        return 1

    merged = dict(seen)
    for key in fetched:
        if delivered or key in seen:
            merged[key] = seen.get(key, now)
    persist_state(args.state, merged, seen)

    for item in to_notify:
        log.info("nuevo: %s | %s", listing_id(item), describe(item).splitlines()[0])
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
