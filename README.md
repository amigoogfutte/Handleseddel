# Handleseddel

Fælles handleseddel og fryser for hele husstanden. Alle enheder ser det samme, fordi alt gemmes i Home Assistant.

- **Seddel**: skriv varer på, kryds af i butikken. Ved hver vare står det, hvis den allerede ligger i fryseren, og hvis den er på tilbud i **Rema 1000, Netto eller Brugsen**. Tryk på tilbuddet for at se pris, periode og billede.
- **Fryser**: delt op i skuffer. Tryk **+** når du lægger noget i og **−** når du tager noget ud. Er en vare brugt op, kan den sættes på sedlen med ét tryk.
- **Fryser-skærm**: en tablet ved fryseren kan vise kun fryseren med store knapper. Skærmen holdes tændt, og den opdaterer hvert 5. sekund.

Tilbuddene hentes fra eTilbudsavis (Tjek) af Home Assistant cirka hver 3. time.

## Installation

1. HACS → ⋮ → **Brugerdefinerede repositories** → tilføj `https://github.com/amigoogfutte/handleseddel` som type **Integration**.
2. Installer **Handleseddel** og genstart Home Assistant.
3. Indstillinger → Enheder og tjenester → **Tilføj integration** → *Handleseddel*.

Appen findes derefter i sidemenuen under **Handleseddel** og på adressen `https://<din-home-assistant>/handle-app/index.html`.
På iPhone: åbn adressen i Safari → Del → **Føj til hjemmeskærm**.

Første gang appen åbnes, henter den varerne fra dine Home Assistant-lister: lister med "Fryser" i navnet bliver til skuffer, og "Indkøbsliste" bliver til sedlen. De gamle lister ændres ikke.

## Tablet ved fryseren

Åbn `https://<din-home-assistant>/handle-app/index.html?skaerm=fryser` på tabletten og føj den til hjemmeskærmen.
Du kan også slå det til under ⚙︎ → **Brug denne enhed som fryser-skærm**. Slå det fra samme sted.
