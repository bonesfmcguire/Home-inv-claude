# Home Inventory

A dead-simple home inventory that you can type to, talk to, paste spreadsheets into, and ask questions through Home Assistant Assist.

- **One box for everything:** type or say `3 AA batteries in the kitchen drawer`, `tent in the attic, stored`, `where are the batteries?`, `what's in the garage`, `used 2 batteries`, or `move the drill to the shed`.
- **Locations and storage:** items are grouped by place. Use `/` for nesting (`Garage / Shelf 2`). Each item has a **stored** flag (packed away vs. in use).
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

**With an AI conversation agent (Claude, OpenAI, Ollama, Gemini…):** the package also defines two scripts, `script.inventory_lookup` and `script.inventory_update`. Expose both under **Settings → Voice assistants → Expose**. The agent then calls them as tools, so free-form questions like *"do we have anything for a camping trip?"* or *"I just put the spare HDMI cables in the TV cabinet"* work too.

You also get **`sensor.inventory_items`**, with attributes for total quantity, number of places, and number of stored items. You can use it on dashboards.

---

## 3. Getting data in

- **Spreadsheet:** select the cells in Excel or Sheets, copy, then click **Import** and paste. Headers are recognised loosely (`Item/Name`, `Qty/Count`, `Location/Where/Room`, `Stored`, `Category`, `Notes`). Without a header row, columns are read as *Name, Location, Quantity, Category, Notes*. `.xlsx` upload also works; it loads a parser from a CDN on first use. If you're offline, save the sheet as CSV.
- **Lists:** paste a list like this:
  ```
  Garage:
  - cordless drill
  - 2 tarps
  - camping tent, stored
  Kitchen:
  - 12 AA batteries in the junk drawer
  ```
- **Voice in the app:** tap the mic. This needs Chrome, Edge or Safari and an HTTPS page; the HA sidebar over HTTPS qualifies.
- **Export:** the **Export** button downloads a CSV that you can edit and re-import. Items with the same name and location are merged and their quantities added together.

## API (for automations and scripts)

| Method | Path | |
|---|---|---|
| GET | `/api/items?q=` | list or search items |
| POST | `/api/items` | add `{name, quantity, location, stored, category, notes}` (or a list of them) |
| PATCH / DELETE | `/api/items/{id}` | update / delete |
| POST | `/api/command` | `{text}`: natural language add/remove/move/ask → `{speech, results}` |
| POST | `/api/ask` | `{text}` or `{item}` or `{location}` → `{speech, results}` |
| POST | `/api/import` | `{text}` (CSV/TSV/list) or `{items: [...]}`; add `?dry_run=1` to preview |
| GET | `/api/stats`, `/api/locations`, `/api/export.csv` | |

## Security note
The app has no login of its own. Through the HA sidebar it is protected by Home Assistant's login. Port 8099 is also exposed on your LAN for direct access and Docker setups. To restrict that, remove the `ports:` section from `home_inventory/config.yaml` (Assist uses HA's internal network and doesn't need it). Don't forward port 8099 to the internet.

## Back up
Everything is in `/data/inventory.db` for the add-on, which is included in HA backups, or `./data/inventory.db` with Docker.
