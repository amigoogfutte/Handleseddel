"""Handleseddel: fælles handleseddel og fryser for husstanden, med tilbud fra eTilbudsavis."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    APP_URL,
    BUTIKKER,
    DOMAIN,
    MAX_BODY_BYTES,
    MAX_RECORD_BYTES,
    PANEL_URL_PATH,
    STORAGE_DIR,
    TILBUD_MAX_ALDER,
    TJEK_API,
)

_LOGGER = logging.getLogger(__name__)
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
WWW = Path(__file__).parent / "www"


class Store:
    """Ét fælles lager for hele husstanden: /config/handleseddel/data.json.

    Indholdet er en samling af poster {id: {...,"updated": ms}}. Ved synkronisering vinder
    den udgave af en post, der senest er ændret. Slettede poster gemmes som {"deleted": true}.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.fil = self.root / "data.json"
        self._laas = threading.Lock()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _laes(self) -> dict[str, dict]:
        if not self.fil.exists():
            return {}
        try:
            d = json.loads(self.fil.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _LOGGER.warning("Kunne ikke læse %s", self.fil)
            return {}

    def _skriv(self, data: dict) -> None:
        tmp = self.fil.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.fil)

    def sync(self, indkomne: list, kun_nye: bool = False) -> dict[str, dict]:
        """Flet poster ind og returnér alle poster. kun_nye: tilføj kun poster, der ikke findes."""
        with self._laas:
            data = self._laes()
            aendret = False
            for r in indkomne:
                if not isinstance(r, dict):
                    continue
                rid = r.get("id")
                upd = r.get("updated")
                if not isinstance(rid, str) or not _ID_RE.match(rid) or not isinstance(upd, (int, float)):
                    continue
                if len(json.dumps(r, ensure_ascii=False)) > MAX_RECORD_BYTES:
                    continue
                cur = data.get(rid)
                if kun_nye and cur is not None:
                    continue
                if cur is None or upd > cur.get("updated", 0):
                    data[rid] = r
                    aendret = True
            if aendret:
                self._skriv(data)
            return data

    # --- gemte tilbud, så appen virker hurtigt og ikke spørger eTilbudsavis hele tiden ---
    def laes_tilbud(self) -> dict | None:
        f = self.root / "tilbud.json"
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except (OSError, ValueError):
            return None

    def skriv_tilbud(self, d: dict) -> None:
        f = self.root / "tilbud.json"
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)


class SyncView(HomeAssistantView):
    """GET: alle poster. POST {"poster":[...]}: flet ind og få alle poster tilbage."""

    url = "/api/handleseddel/sync"
    name = "api:handleseddel:sync"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        data = await self.hass.async_add_executor_job(self.store.sync, [])
        return self.json({"poster": list(data.values())})

    async def post(self, request: web.Request) -> web.Response:
        body = await request.read()
        if len(body) > MAX_BODY_BYTES:
            return self.json_message("For stor", 413)
        try:
            obj = json.loads(body)
        except ValueError:
            return self.json_message("Ugyldig JSON", 400)
        poster = obj.get("poster") if isinstance(obj, dict) else None
        if not isinstance(poster, list):
            return self.json_message("Forkert indhold", 400)
        data = await self.hass.async_add_executor_job(self.store.sync, poster)
        return self.json({"poster": list(data.values())})


# --- import fra Home Assistants egne lister (todo) ---

_TAELLE_ENHEDER = {
    "stk": "stk", "stk.": "stk", "pose": "pose", "poser": "pose", "pakke": "pakke", "pakker": "pakke",
    "bakke": "bakke", "bakker": "bakke", "æske": "æske", "æsker": "æske", "rulle": "rulle", "ruller": "rulle",
}


def _fortolk(tekst: str) -> tuple[str, float, str]:
    """"Rundstykker 4 poser" -> ("Rundstykker", 4, "pose"); "2 fiskefileter" -> ("Fiskefileter", 2, "").

    Vægt (kg, g) bliver stående i navnet, fordi det beskriver varen og ikke antallet.
    """
    s = " ".join((tekst or "").split())
    m = re.match(r"^(.*?)\s+(?:ca\.?\s+)?(\d+(?:[.,]\d+)?)\s*([A-Za-zæøåÆØÅ.]+)?$", s)
    if m and m.group(1):
        enhed = (m.group(3) or "").lower()
        if enhed in _TAELLE_ENHEDER:
            return m.group(1).strip(), float(m.group(2).replace(",", ".")), _TAELLE_ENHEDER[enhed]
    m = re.match(r"^(\d+)\s+([^\d/].*)$", s)
    if m:
        navn = m.group(2).strip()
        return navn[:1].upper() + navn[1:], float(m.group(1)), ""
    return s, 1.0, ""


def _rent_id(prefix: str, raw: str) -> str:
    return (prefix + re.sub(r"[^A-Za-z0-9_.:-]", "", raw or ""))[:120]


class ImportView(HomeAssistantView):
    """POST: hent varer fra Home Assistants lister (Fryser-… og Indkøbsliste) én gang.

    Lister hvis navn indeholder "fryser" bliver til skuffer i fryseren, "Indkøbsliste" bliver til sedlen.
    Varer, der allerede er hentet, hentes ikke igen.
    """

    url = "/api/handleseddel/importer"
    name = "api:handleseddel:importer"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def post(self, request: web.Request) -> web.Response:
        nu = int(time.time() * 1000)
        poster: list[dict] = []
        skuffer = fryser = seddel = 0
        stater = sorted(self.hass.states.async_all("todo"), key=lambda s: s.entity_id)
        for st in stater:
            navn = str(st.attributes.get("friendly_name") or st.entity_id)
            lav = navn.lower()
            er_fryser = "fryser" in lav
            er_seddel = (not er_fryser) and ("indkøb" in lav or "shopping" in lav or "handle" in lav)
            if not er_fryser and not er_seddel:
                continue
            try:
                svar = await self.hass.services.async_call(
                    "todo", "get_items", {"entity_id": st.entity_id, "status": ["needs_action"]},
                    blocking=True, return_response=True,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Kunne ikke hente %s", st.entity_id)
                continue
            items = ((svar or {}).get(st.entity_id) or {}).get("items") or []
            if er_fryser:
                sk_navn = re.sub(r"^\s*fryser\s*[-–:]?\s*", "", navn, flags=re.I).strip() or navn
                sk_navn = sk_navn[:1].upper() + sk_navn[1:]
                sk_id = _rent_id("sk-", st.entity_id.split(".", 1)[1])
                poster.append({"id": sk_id, "kind": "skuffe", "navn": sk_navn, "orden": skuffer, "updated": nu})
                skuffer += 1
                for it in items:
                    n, antal, enhed = _fortolk(str(it.get("summary") or ""))
                    if not n:
                        continue
                    poster.append({"id": _rent_id("f-", it.get("uid") or n), "kind": "frys", "navn": n,
                                   "antal": antal, "enhed": enhed, "skuffe": sk_id, "lagtInd": nu, "updated": nu})
                    fryser += 1
            else:
                for it in items:
                    n = " ".join(str(it.get("summary") or "").split())
                    if not n:
                        continue
                    poster.append({"id": _rent_id("v-", it.get("uid") or n), "kind": "vare", "navn": n,
                                   "taget": False, "oprettet": nu, "updated": nu})
                    seddel += 1
        data = await self.hass.async_add_executor_job(lambda: self.store.sync(poster, kun_nye=True))
        return self.json({"skuffer": skuffer, "fryser": fryser, "seddel": seddel, "poster": list(data.values())})


# --- tilbud fra eTilbudsavis ---

def _slank(o: dict, butik: str) -> dict | None:
    """Behold kun det, appen bruger."""
    try:
        pris = (o.get("pricing") or {}).get("price")
        q = o.get("quantity") or {}
        size = q.get("size") or {}
        unit = ((q.get("unit") or {}).get("symbol")) or ""
        return {
            "id": o.get("id"),
            "butik": butik,
            "titel": (o.get("heading") or "").strip(),
            "tekst": (o.get("description") or "").strip()[:400],
            "pris": pris,
            "foer": (o.get("pricing") or {}).get("pre_price"),
            "fra": o.get("run_from"),
            "til": o.get("run_till"),
            "billede": ((o.get("images") or {}).get("thumb")) or "",
            "maengde": {"fra": size.get("from"), "til": size.get("to"), "enhed": unit},
        }
    except (AttributeError, TypeError):
        return None


class Tilbud:
    """Henter og gemmer aktuelle tilbud for de valgte butikker."""

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store
        self._laas = asyncio.Lock()
        self._cache: dict | None = None

    async def _hent_butik(self, butik: str, dealer_id: str) -> list[dict]:
        session = async_get_clientsession(self.hass)
        lat = self.hass.config.latitude or 55.68
        lng = self.hass.config.longitude or 12.57
        ud: list[dict] = []
        for offset in range(0, 3000, 100):
            params = {"dealer_ids": dealer_id, "r_lat": str(lat), "r_lng": str(lng), "r_radius": "30000",
                      "limit": "100", "offset": str(offset)}
            async with session.get(TJEK_API, params=params, timeout=30,
                                   headers={"Accept": "application/json"}) as r:
                if r.status != 200:
                    raise RuntimeError(f"eTilbudsavis svarede {r.status}")
                side = await r.json(content_type=None)
            if not isinstance(side, list) or not side:
                break
            ud += [x for x in (_slank(o, butik) for o in side if isinstance(o, dict)) if x and x.get("id")]
            if len(side) < 100:
                break
        return ud

    async def hent(self, frisk: bool = False) -> dict:
        async with self._laas:
            if self._cache is None:
                self._cache = await self.hass.async_add_executor_job(self.store.laes_tilbud)
            c = self._cache
            if c and not frisk and time.time() - c.get("hentet", 0) < TILBUD_MAX_ALDER:
                return c
            alle: list[dict] = []
            fejl: list[str] = []
            for butik, (dealer_id, navn) in BUTIKKER.items():
                try:
                    alle += await self._hent_butik(butik, dealer_id)
                except Exception as e:  # noqa: BLE001
                    _LOGGER.warning("Kunne ikke hente tilbud fra %s: %s", navn, e)
                    fejl.append(navn)
                    # behold de gamle tilbud for butikken, hvis vi har dem
                    if c:
                        alle += [t for t in c.get("tilbud", []) if t.get("butik") == butik]
            if not alle and c:
                return dict(c, fejl=fejl)
            ny = {"hentet": time.time(), "tilbud": alle, "fejl": fejl,
                  "butikker": {k: v[1] for k, v in BUTIKKER.items()}}
            self._cache = ny
            await self.hass.async_add_executor_job(self.store.skriv_tilbud, ny)
            return ny


class TilbudView(HomeAssistantView):
    """GET: aktuelle tilbud fra Rema 1000, Netto og Brugsen. ?frisk=1 henter forfra."""

    url = "/api/handleseddel/tilbud"
    name = "api:handleseddel:tilbud"
    requires_auth = True

    def __init__(self, tilbud: Tilbud) -> None:
        self.tilbud = tilbud

    async def get(self, request: web.Request) -> web.Response:
        d = await self.tilbud.hent(frisk=request.query.get("frisk") == "1")
        return self.json(d)


# --- læs et tilbud fra et billede med Home Assistants AI-opgave (fx Google Gemini) ---

STRUKTUR = {
    "navn": {"description": "Varens navn kort og som man ville skrive det på en indkøbsliste, fx 'Kaffe' eller 'Hakket oksekød'. Uden mærke, medmindre mærket er det vigtige.", "required": True, "selector": {"text": {}}},
    "fulde_navn": {"description": "Varens fulde navn som det står på tilbuddet, med mærke og størrelse.", "required": False, "selector": {"text": {}}},
    "pris": {"description": "Tilbudsprisen i kroner som tal, fx 35 eller 9.95. Tom hvis den ikke kan ses.", "required": False, "selector": {"number": {"mode": "box", "step": 0.01}}},
    "foer_pris": {"description": "Normalprisen/førprisen i kroner, hvis den står der.", "required": False, "selector": {"number": {"mode": "box", "step": 0.01}}},
    "butik": {"description": "Butikken: 'netto', 'rema', 'brugsen' eller 'anden'. Netto+ og personlige tilbud i Netto-appen er 'netto'.", "required": False, "selector": {"select": {"options": ["netto", "rema", "brugsen", "anden"]}}},
    "butik_navn": {"description": "Butikkens navn hvis butik er 'anden'.", "required": False, "selector": {"text": {}}},
    "slutdato": {"description": "Sidste dag tilbuddet gælder, som ÅÅÅÅ-MM-DD. Regn den ud fra dagens dato, hvis der står fx 'slutter om 3 dage' eller 'gælder til søndag'. Tom hvis det ikke står.", "required": False, "selector": {"text": {}}},
    "note": {"description": "Kort vigtig betingelse, fx 'max 2 stk.', 'ved køb af 3', 'kræver Netto+'. Tom hvis ingen.", "required": False, "selector": {"text": {}}},
    "boks_x1": {"description": "Venstre kant af varens produktbillede som andel af billedets bredde (0 til 1).", "required": False, "selector": {"number": {"min": 0, "max": 1, "step": 0.001, "mode": "box"}}},
    "boks_y1": {"description": "Øverste kant af varens produktbillede som andel af billedets højde (0 til 1).", "required": False, "selector": {"number": {"min": 0, "max": 1, "step": 0.001, "mode": "box"}}},
    "boks_x2": {"description": "Højre kant af varens produktbillede som andel af billedets bredde (0 til 1).", "required": False, "selector": {"number": {"min": 0, "max": 1, "step": 0.001, "mode": "box"}}},
    "boks_y2": {"description": "Nederste kant af varens produktbillede som andel af billedets højde (0 til 1).", "required": False, "selector": {"number": {"min": 0, "max": 1, "step": 0.001, "mode": "box"}}},
}


class LaesView(HomeAssistantView):
    """POST {"billede": "<base64 jpeg>"}: læs vare, pris, slutdato og produktbilledets placering."""

    url = "/api/handleseddel/laes"
    name = "api:handleseddel:laes"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        import base64
        import datetime as dt
        import uuid

        entiteter = self.hass.states.async_entity_ids("ai_task")
        if not entiteter:
            return self.json_message("Ingen AI sat op i Home Assistant (fx Google Gemini).", 409)
        body = await request.read()
        if len(body) > 8 * 1024 * 1024:
            return self.json_message("Billedet er for stort", 413)
        try:
            raw = base64.b64decode(json.loads(body)["billede"].split(",")[-1])
        except (ValueError, KeyError, TypeError):
            return self.json_message("Ugyldigt billede", 400)
        media_root = Path(self.hass.config.media_dirs.get("local") or self.hass.config.path("media"))
        mappe = media_root / "handleseddel"
        navn = f"{uuid.uuid4().hex}.jpg"

        def gem() -> None:
            mappe.mkdir(parents=True, exist_ok=True)
            (mappe / navn).write_bytes(raw)

        await self.hass.async_add_executor_job(gem)
        idag = dt.date.today()
        ugedag = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"][idag.weekday()]
        async def spoerg() -> dict:
            return await self.hass.services.async_call(
                "ai_task", "generate_data",
                {
                    "task_name": "Handleseddel: læs tilbud",
                    "entity_id": entiteter[0],
                    "instructions": (
                        f"I dag er {ugedag} {idag.isoformat()}. Billedet er et tilbud fra en dansk dagligvarebutik, "
                        "ofte et skærmbillede af et personligt tilbud i Netto-appen (Netto+), eller et foto af et skilt/en avis. "
                        "Find det ene tilbud, der er i fokus, og udfyld felterne. Svar på dansk. "
                        "Boksen skal omslutte selve produktbilledet (pakken/varen), ikke tekst og pris."
                    ),
                    "structure": STRUKTUR,
                    "attachments": [{"media_content_id": f"media-source://media_source/local/handleseddel/{navn}",
                                     "media_content_type": "image/jpeg"}],
                },
                blocking=True, return_response=True,
            )

        try:
            try:
                svar = await spoerg()
            except Exception:  # noqa: BLE001
                # Google svarer af og til "travlt" (503) – prøv én gang til
                await asyncio.sleep(3)
                svar = await spoerg()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("AI kunne ikke læse billedet: %s", e)
            return self.json_message(f"AI kunne ikke læse billedet: {e}", 502)
        finally:
            await self.hass.async_add_executor_job(lambda: (mappe / navn).unlink(missing_ok=True))
        data = (svar or {}).get("data")
        if not isinstance(data, dict):
            return self.json_message("AI gav intet svar", 502)
        return self.json({"tilbud": data})


class AppRedirectView(HomeAssistantView):
    """Kort adresse: /handleseddel-start → appen."""

    url = "/handleseddel-start"
    name = "handleseddel:start"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{APP_URL}/index.html")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sæt integrationen op."""
    store = Store(hass.config.path(STORAGE_DIR))
    await hass.async_add_executor_job(store.ensure)

    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("http_registered"):
        # HTTP-ruter kan ikke fjernes igen, så de registreres kun én gang pr. opstart.
        await hass.http.async_register_static_paths([StaticPathConfig(APP_URL, str(WWW), False)])
        tilbud = Tilbud(hass, store)
        for view in (SyncView(hass, store), ImportView(hass, store), TilbudView(tilbud), LaesView(hass), AppRedirectView()):
            hass.http.register_view(view)
        data["http_registered"] = True

    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Handleseddel",
        sidebar_icon="mdi:cart-outline",
        frontend_url_path=PANEL_URL_PATH,
        config={"url": f"{APP_URL}/index.html"},
        require_admin=False,
        update=True,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fjern sidepanelet."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return True
