# Home Inventory

A dead-simple home inventory that you can type to, talk to, paste spreadsheets into, and ask questions through Home Assistant Assist.

- **One box for everything:** type or say `3 AA batteries in the kitchen drawer`, `tent in the attic, stored`, `where are the batteries?`, `what's in the garage`, `used 2 batteries`, or `move the drill to the shed`.
- **Lists, not just single items:** `drill, saw and 2 tarps in the garage` adds three items. Paste several lines and you get a review screen first, where you can skip any row before adding.
- **Locations and storage:** items are grouped by place. Use `/` for nesting (`Garage / Shelf 2`). Places are **case-insensitive**: `living room`, `Living Room` and `LIVING ROOM` are the same place, and a place keeps the spelling you already use. The pencil on a place renames it. Each item has a **stored** flag (packed away vs. in use).
- **Walk-around dictation:** walk into a room, say *"start inventory"* to a voice satellite (or tap **Dictate** in the app), and name things as you see them: *"a toaster, two mugs and a blender"*. The room comes from the satellite's area in Home Assistant, so everything lands there. *"Batteries in the junk drawer"* is filed as `Kitchen / Junk drawer`. Say *"undo"* to take back the last thing and *"done"* to finish.
- **Purchase details and photos:** optional price, purchase date, where it was bought, brand/model, serial number, warranty date and a photo. The list shows thumbnails and total value, and you can ask "how much did the TV cost?"
- **Bulk input:** paste cells straight from Excel or Google Sheets, upload a CSV/TSV/XLSX, or paste a plain list. You see a preview before anything is imported.
- **Home Assistant:** runs as an add-on in your sidebar. Assist voice intents and LLM tools let you ask "where are my AA batteries?" from any voice satellite.
- **No dependencies:** one Python file plus SQLite. Your data is a single `inventory.db` file.

```
home_inventory/        ← the app (also the HA add-on folder)
  server.py            ← API + natural-language parsing (stdlib only)
  static/index.html    ← the whole UI
  config.yaml, Dockerfile
homeassistant/
  packages/home_inventory.yaml                 ← REST commands, intents, LLM tool scripts, sensor
  custom_sentences/en/home_inventory.yaml      ← Assist voice sentences
examples/sample.csv
repository.yaml        ← makes this repo installable as an add-on repository
docker-compose.yml
```

---

## 1. Install

### Option A: Home Assistant add-on (HA OS / Supervised)

**Local add-on (quickest):**
1. Copy the `home_inventory/` folder into `/addons/` on your HA box. You can use the *Samba share* or *Studio Code Server* add-on for this.
2. Go to **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**. "Home Inventory" appears under **Local add-ons**.
3. Install it, turn on **Show in sidebar**, and start it.

**From GitHub:** push this repo, go to **Add-on Store → ⋮ → Repositories** and add `https://github.com/bonesfmcguire/Home-inv-claude`.

The UI opens from the sidebar through HA ingress, so you're already logged in and it works remotely over Nabu Casa or your own HTTPS.

### Option B: Docker (HA Container or any server)
```bash
docker compose up -d --build
```
Open `http://<server>:8099`. The data lives in `./data/inventory.db`.

### Option C: Just run it
```bash
python3 home_inventory/server.py
```
Use the `PORT` and `DATA_DIR` environment variables to change where it listens and stores data.

---

## 2. Connect Home Assistant Assist

1. Enable packages in `configuration.yaml`, if you haven't already:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Copy `homeassistant/packages/home_inventory.yaml` to `config/packages/`.
3. Copy `homeassistant/custom_sentences/en/home_inventory.yaml` to `config/custom_sentences/en/`.
4. Check the URL in the package file (search for `8099`):
   - local add-on: `http://local-home-inventory:8099` (the default)
   - add-on installed from a GitHub repo: use the **Hostname** shown on the add-on's info page
   - Docker or another machine: `http://<ip>:8099`
5. Restart Home Assistant.

**What you can say (built-in Assist, no AI needed):**

| Say | Result |
|---|---|
| "Where are the AA batteries?" / "Where did I put the drill?" / "Do I have a ladder?" | Finds the item |
| "How many AA batteries do we have?" | Gives the total |
| "What's in the attic?" / "What do I have in storage?" | Lists items in a place |
| "Add 3 light bulbs in the pantry" / "I stored the tent in the attic" | Adds items (the second one is marked stored) |
| "I used 2 batteries" / "I threw away the old printer" | Lowers the quantity or removes the item |
| "Move the drill to the shed" | Changes the item's location |
| "How much did the TV cost?" / "Where did I buy the drill?" | Reads back price, store and date |

**With an AI conversation agent (Claude, OpenAI, Ollama, Gemini…):** the package also defines two scripts, `script.inventory_lookup` and `script.inventory_update`. Expose both under **Settings → Voice assistants → Expose**. The agent then calls them as tools, so free-form questions like *"do we have anything for a camping trip?"* or *"I just put the spare HDMI cables in the TV cabinet"* work too.

You also get **`sensor.inventory_items`**, with attributes for total quantity, number of places, and number of stored items. You can use it on dashboards.

---

### Dictating a room (hands-free, no wake word between items)

Say **"start inventory"** (or *"start inventory in the garage"*, *"inventory this room"*) to a voice satellite. The satellite replies *"Okay, adding to the Kitchen."* and then keeps listening and confirming each phrase:

> **You:** "Start inventory" · **Assist:** "Okay, adding to the Kitchen. What do you see?"
> **You:** "A toaster, two mugs and a blender" · **Assist:** "Added Toaster, 2 Mugs and Blender."
> **You:** "Three AA batteries in the junk drawer" · **Assist:** "Added 3 AA batteries."
> **You:** "Scratch that" · **Assist:** "Removed 3 AA batteries."
> **You:** "Done" · **Assist:** "Done. Added 3 items in the Kitchen."

How the room is chosen: the satellite's **area** in Home Assistant (Settings → Devices → your satellite → area). Saying a different room ("start inventory in the garage") overrides it, and while dictating, "in the garage" files an item in the garage even if you're standing in the kitchen. Two silences in a row end the session, and sessions expire after 30 minutes idle.

What it needs, and what I could and couldn't check:
- Home Assistant **2025.7 or newer** and a satellite that supports `assist_satellite.ask_question` (Home Assistant Voice Preview Edition and ESPHome satellites do; some basic Wyoming satellites don't).
- The package from step 2 above (it adds the sentence, three REST commands and the `inventory_dictation_step` script). If you keep your automations in the UI rather than YAML and `automation:` in a package gives you trouble, paste the `automation:` entry from the package into a new automation in YAML mode.
- I tested the server side and the in-app Dictate mode end to end, and rendered the package's templates and posted its REST payloads to the real server. I could not run this against a real Home Assistant instance or satellite, so expect to tweak the YAML on first use. Open Settings → Automations & scenes → *Inventory dictation - start* → Traces if it doesn't respond.

No satellite in the room? Open the app on your phone, tap **Dictate**, pick the room and talk. Typing into the box works too.

## 3. Getting data in

- **Spreadsheet:** select the cells in Excel or Sheets, copy, then click **Add list** and paste. Headers are recognised loosely: `Item/Name`, `Qty/Count`, `Location/Where/Room`, `Stored`, `Category`, `Price/Cost`, `Purchase date/Acquired`, `Store/Purchased from`, `Brand/Model`, `Serial`, `Warranty`, `Notes`. Without a header row, tab-separated columns are read as *Name, Location, Quantity, Category, Notes*. `.xlsx` upload also works; it loads a parser from a CDN on first use. If you're offline, save the sheet as CSV.
- **Lists:** paste a list like this:
  ```
  Garage:
  - cordless drill
  - 2 tarps
  - camping tent, stored
  Kitchen:
  - 12 AA batteries in the junk drawer
  ```
  Several items on one line are split on commas and on "and" when it is followed by a quantity or "a/the" (`2 tarps, a ladder and 3 hooks`). A plain "salt and pepper" stays one item.
- **Buying something:** `I bought a TV from Best Buy for $499 and put it in the living room` fills in price, store and location. You can also add `on 2024-03-01`, `today`, or `warranty until 2027-05-01`. Price is per item.
- **Photos:** open an item and choose **Add photo**. Photos are shrunk in the browser before upload and stored in `/data/photos` next to the database.
- **Voice in the app:** tap the mic. This needs Chrome, Edge or Safari and an HTTPS page; the HA sidebar over HTTPS qualifies.
- **Export:** the **Export** button downloads a CSV that you can edit and re-import. Items with the same name and location are merged and their quantities added together.

## API (for automations and scripts)

| Method | Path | |
|---|---|---|
| GET | `/api/items?q=` | list or search items |
| POST | `/api/items` | add `{name, quantity, location, stored, category, notes, price, purchase_date, purchased_from, brand, serial, warranty_until}` (or a list of them) |
| POST / DELETE | `/api/items/{id}/photo` | upload the raw image bytes (JPEG/PNG/WebP/GIF) / remove it. `?thumb=1` uploads the JPEG thumbnail. Photos are served from `photos/{file}` |
| POST | `/api/locations/rename` | `{from, to}`: rename a place and everything inside it |
| PATCH / DELETE | `/api/items/{id}` | update / delete |
| POST | `/api/command` | `{text}`: natural language add/remove/move/ask → `{speech, results}`. `dry_run: true` returns what an add would create without saving |
| POST | `/api/ask` | `{text}` or `{item}` or `{location}` → `{speech, results}`. `{item, about: "purchase"}` answers price / store / date |
| POST | `/api/import` | `{text}` (CSV/TSV/list) or `{items: [...]}`; add `?dry_run=1` to preview |
| POST | `/api/session/start`, `/api/session/say`, `/api/session/end` | dictation: `{id, area}` starts a session for a room; `{id, text}` adds what was said (also understands *undo* and *done*); `{id}` ends it |
| GET | `/api/stats` (includes total `value`), `/api/locations`, `/api/export.csv` | |

## Security note
The app has no login of its own. Through the HA sidebar it is protected by Home Assistant's login. Port 8099 is also exposed on your LAN for direct access and Docker setups. To restrict that, remove the `ports:` section from `home_inventory/config.yaml` (Assist uses HA's internal network and doesn't need it). Don't forward port 8099 to the internet.

## Back up
Everything is in `/data` for the add-on (the database plus a `photos/` folder), which is included in HA backups, or `./data` with Docker. Photos make backups larger.

## Upgrading from 1.0
Just update and restart. Existing items keep working: the database gets the new columns automatically, and on startup places that differ only by capitalisation (`Living room` / `Living Room`) are merged into the spelling most of your items already use.
