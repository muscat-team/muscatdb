# Target Tagging, Grouping, Project Pages & Index Table Streamlining Plan

## 1. Executive Summary

This plan outlines the architecture and implementation strategy for adding **Target Tagging & Project Management** to MuscatDB, while streamlining the homepage index table.

Key Features:
- **Projects Hub in Navbar:** A top navbar item **Projects** placed before `Target` (`MuSCAT-db` → `ObsLog` → `Projects` → `Target`).
- **Projects Directory (`/projects` / `/tags`):** Top-level directory of active projects/tags with project cards, target counts, descriptions, and a **+ Create New Project** modal/button.
- **Project Detail Page (`/tag?name=<project_name>`):**
  - **Editable Description Area:** Above the summary table with inline **Edit / Save / Cancel** controls.
  - **Add Target Search Bar:** Interactive target search box with autocomplete to attach targets to the project directly.
  - **Export CSV Button:** Download CSV summary of project targets and metrics.
  - **Streamlined Summary Table (6 columns):** Target Name (link to `/target?name=...&from_tag=...`), Dates, Ndataset, Filters, # Frames, Actions (detach target).
- **Context-Aware Navigation on `/target` Page:**
  - When navigating to a target from a project page (`/tag?name=Project-Alpha`), the back link reads `← Back to project Project-Alpha` (linking back to `/tag?name=Project-Alpha`).
  - When navigating from the homepage search, the back link reads `← Back to database search` (linking to `/`).
- **Master Table Tagging & Streamlining (`index.html`):**
  - Added **Tags** column with clickable chips and inline `+ Tag` quick adder.
  - Removed **RA**, **Dec**, **Airmass**, and **Move** columns for a clean 7-column layout.
- **Database Persistence:**
  - SQLite tables `target_tags` and `tag_descriptions` registered in `_APP_OWNED_TABLES` (preserves all tags and descriptions across nightly `build_db` database rebuilds).

---

## 2. Table Column Specifications

### Homepage Master Table (`index.html`) — 7 Columns
1. **Object** (raw OBJECT header name)
2. **Normalized Target** (links to `/target?name=<norm_name>`)
3. **Dates** (observed YYMMDD list with instrument codes)
4. **Ndataset** (dataset count)
5. **Filters** (filter chips)
6. **# Frames** (total frames count)
7. **Tags** *(NEW interactive tag badges + `+ Tag` adder)*
*(RA, Dec, Airmass, and Move columns removed)*

### Tag/Project Summary Table (`/tag?name=<project_name>`) — 6 Columns
1. **Target Name** (links to `/target?name=<norm_name>&from_tag=<project_name>`)
2. **Dates** (observed YYMMDD list with instrument codes)
3. **Ndataset** (total dataset count)
4. **Filters** (filter chips)
5. **# Frames** (total frames taken for target)
6. **Actions** (detach target button)

---

## 3. Database Schema Changes (`src/muscat_db/database.py`)

Add the following tables to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS target_tags (
    object     TEXT NOT NULL,
    tag        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (object, tag)
);
CREATE INDEX IF NOT EXISTS idx_target_tags_tag ON target_tags(tag);
CREATE INDEX IF NOT EXISTS idx_target_tags_object ON target_tags(object);

CREATE TABLE IF NOT EXISTS tag_descriptions (
    tag         TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Register `"target_tags"` and `"tag_descriptions"` in `_APP_OWNED_TABLES`:
```python
_APP_OWNED_TABLES = (
    ...
    "target_tags",
    "tag_descriptions",
)
```

Database Helper Functions to Implement in `database.py`:
- `get_all_target_tags(db_path: str) -> dict[str, list[str]]`
- `add_target_tag(db_path: str, object: str, tag: str) -> None`
- `remove_target_tag(db_path: str, object: str, tag: str) -> None`
- `delete_project_tag(db_path: str, tag: str) -> None`
- `get_targets_for_tag(db_path: str, tag: str) -> list[dict]`
- `get_tag_description(db_path: str, tag: str) -> str`
- `set_tag_description(db_path: str, tag: str, description: str) -> None`

---

## 4. Backend Web Routes (`src/muscat_db/web.py`)

### HTML Page Routes
- `@app.get("/projects", response_class=HTMLResponse)` and `@app.get("/tags", response_class=HTMLResponse)`: Renders `projects.html` (Projects Directory).
- `@app.get("/tag", response_class=HTMLResponse)`: Renders `tag.html` (Project detail view for query param `name`).

### API Endpoints (`target_router` / `tag_router`)
- `GET /api/targets/{obj}/tags`: Return JSON `{"ok": True, "target": obj, "tags": [...]}`
- `POST /api/targets/{obj}/tags`: Add tag (body: `{"tag": "..."}`)
- `DELETE /api/targets/{obj}/tags/{tag}`: Remove tag from target
- `GET /api/tags`: List all active tags with target counts and descriptions
- `POST /api/tags`: Create a new project tag (body: `{"tag": "...", "description": "..."}`)
- `DELETE /api/tags/{tag}`: Delete a project tag
- `GET /api/tags/{tag}/description`: Get project description
- `PUT /api/tags/{tag}/description`: Save project description (body: `{"description": "..."}`)
- `GET /api/tags/{tag}/export.csv`: Export project CSV summary

---

## 5. Template & Frontend Changes

1. **`src/muscat_db/templates/base.html`:**
   - Add `<a href="/projects" data-nav-section="projects">Projects</a>` directly before `<a id="target-nav-link" href="/target" data-nav-section="target">Target</a>`.
   - Update JS navigation active section highlight for `/projects`, `/tags`, and `/tag`.

2. **`src/muscat_db/templates/projects.html` [NEW]:**
   - Directory page rendering project cards grid with target counts, date counts, description preview, **View**, **Edit Description**, and **Delete Project Tag** buttons, and a **+ Create New Project** modal.

3. **`src/muscat_db/templates/tag.html` [NEW]:**
   - Project Detail page displaying:
     - Header metrics & Export CSV button.
     - Editable description container (Save/Cancel).
     - Add Target to Project search input box with autocomplete.
     - Streamlined 6-column summary table.

4. **`src/muscat_db/templates/target.html` [MODIFY]:**
   - Read `from_tag` query parameter / `document.referrer`.
   - Render `← Back to project <from_tag>` when coming from a project page, else `← Back to database search`.

5. **`src/muscat_db/templates/index.html` [MODIFY]:**
   - Remove `RA`, `Dec`, `Airmass`, and `Move` headers, filter inputs, and data cells.
   - Add `Tags` column with interactive chips and `+ Tag` adder.

6. **`src/muscat_db/static/styles.css` [MODIFY]:**
   - Add styling for project cards (`.projects-grid`, `.project-card`), tag chips (`.tag-chip`), description editors (`.tag-description-box`), and modals.

---

## 6. Verification & Automated Tests Plan

1. **Test File:** `tests/test_tags.py`
2. **Tests to Include:**
   - Database functions (`add_target_tag`, `remove_target_tag`, `delete_project_tag`, `set_tag_description`).
   - Rebuild preservation (`_APP_OWNED_TABLES`).
   - API endpoints (`GET/POST/DELETE` for tags and descriptions, CSV export).
   - Page route rendering (`/projects`, `/tag?name=FollowUp`).
   - Context-aware back link parameter on `/target`.
3. **Execution Command:**
   ```bash
   uv run pytest tests/test_tags.py
   uv run pytest tests/test_web.py
   ```
