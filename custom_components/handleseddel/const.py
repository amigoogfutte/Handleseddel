"""Konstanter for Handleseddel."""

DOMAIN = "handleseddel"
APP_URL = "/handle-app"
PANEL_URL_PATH = "handleseddel"
STORAGE_DIR = "handleseddel"
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024

# Butikker i eTilbudsavis (Tjek). Nøgle -> (forhandler-id, navn)
BUTIKKER = {
    "rema": ("11deC", "Rema 1000"),
    "netto": ("9ba51", "Netto"),
    "brugsen": ("d311fg", "Brugsen"),
}
TJEK_API = "https://squid-api.tjek.com/v2/offers"
TILBUD_MAX_ALDER = 3 * 60 * 60  # sekunder før tilbud hentes igen
