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
        for view in (SyncView(hass, store), ImportView(hass, store), TilbudView(tilbud), AppRedirectView()):
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
