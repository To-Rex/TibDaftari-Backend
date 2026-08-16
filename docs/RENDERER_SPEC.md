# Result-document renderer — exact spec (port of the frontend DocumentRenderer)

Source of truth (frontend, Clinic-Web): `src/domain/template.ts`, `src/domain/template-render.ts`,
`src/domain/catalog.ts` (`evaluateNumber`, `referenceText`), `src/features/documents/buildContext.ts`,
`src/features/documents/DocumentRenderer.tsx`, `src/shared/lib/format.ts`.

Unit system: **1 unit = 1 CSS px @ 96 dpi**. PDF: `pt = px * 0.75`. A4 portrait = 794×1123 px.

## 1. RenderContext

```
RenderContext {
  items?: RenderItem[]                 // only for order-scoped documents
  patient: { fullName, phone, birthDate?, age?, gender?, address?, passportNumber?, genderRaw?: 'male'|'female', ageMonths?: number }
  order:   { number, date }
  item:    { serviceName, approvedAt?, technician?, doctor?, labNote? }
  company: { name, phone?, address? }
  branch:  { name, address? }
  category:{ name, phone? }            // department; category.phone = legacy tel_lab
  today: string
  values: ValueMap                     // raw values of the primary item
  schema: AttributeSchema | null       // schema of the primary item
}
RenderItem { code (ServiceType.code or serviceTypeId), serviceTypeId, serviceName, status (raw enum), values, schema, approvedAt? (already 'dd.MM.yyyy HH:mm'), technician?, doctor? }
```
No `employee` key. Doctor is `{item.doctor}`.

Filling (buildRenderContext):
| key | value |
|---|---|
| patient.fullName | fullName or '' |
| patient.phone | fmtPhone(phone) — **'—' when empty** |
| patient.birthDate | fmtDate → dd.MM.yyyy — **'—' when absent** |
| patient.age | full years as string, no suffix; '' when no birthDate |
| patient.gender | label by language: uz Erkak/Ayol, ru Мужской/Женский, en Male/Female (backend: use template.language) |
| patient.genderRaw / ageMonths | raw enum / integer months |
| patient.address | [districtName, street].filter(Boolean).join(', ') (region NOT included); '' when empty |
| patient.passportNumber | raw or '' |
| order.number | e.g. UR-000123 |
| order.date | fmtDate(order.createdAt) dd.MM.yyyy ('—' when none) |
| item.serviceName | '' default |
| item.approvedAt | fmtDateTime dd.MM.yyyy HH:mm, '' when not approved |
| item.technician / doctor / labNote | names / note or '' |
| company.name/phone/address, branch.name/address, category.name/phone | raw or '' |
| today | dd.MM.yyyy (Asia/Tashkent) |

Formatters: `fmtDate(iso)` = dd.MM.yyyy else '—'; `fmtDateTime` = dd.MM.yyyy HH:mm; `fmtPhone(p)`: !p → '—'; digits = re.sub(\D); if 12 digits starting '998' → `+998 90 123-45-67` (`+998 {d[3:5]} {d[5:8]}-{d[8:10]}-{d[10:]}`), else original string. Times: pin timezone Asia/Tashkent.

### values stringification — formatValue(ctx, key)
```
raw = values[key]; def = schema field by key
raw is None or raw == ''  -> ''
no def                    -> fmt(raw)
select      -> option label (by value) or str(raw)
multiselect -> [label or v for v in raw].join(', ')   (guard non-array → fmt)
boolean     -> raw ? (trueLabel or 'Ha') : (falseLabel or 'Yo‘q')   # U+2018 in Yo‘q
number      -> if numeric: format with decimals (default 0, ROUND_HALF_UP like JS toFixed) then strip /\.0+$/ ; else str(raw)
default (text|longtext|date|table) -> fmt(raw)
```
`fmt(v)`: None → ''; list → map(fmt).join(', '); bool → '✓' / '—'; else str(v).
Consequences: date values are printed as stored ISO `yyyy-MM-dd`; number 12.00→"12", 12.30→"12.30"; `{values.<tableKey>}` in JS gives "[object Object], …" — backend: return '' for table-typed values (documented divergence).

### items dataset — itemRows(ctx)
Row per RenderItem i: `{ i: i+1, code, name: serviceName, status, ...for each non-table field f of item schema: row[f.key] = formatValue(item values/schema, f.key) }`.
`tableRows(ctx, fieldKey)`: 'items' → itemRows; else `values[fieldKey]` if list else [] (raw row objects, NOT formatted).

### svc.<CODE>.<field> — serviceValue(ctx, path)
Split at LAST dot: code = path[:dot], key = path[dot+1:]. Item = items where code.strip().lower() == code.strip().lower(); none → ''.
key 'name' → serviceName; 'status' → raw status; 'approvedAt' → item.approvedAt or ''; 'technician'/'doctor' → or ''; else formatValue(item values/schema, key).

## 2. Placeholders — interpolate(text, ctx, row=None)
Regex `\{([a-zA-Z0-9_.\-]+)\}` — no spaces/pipes/filters/escaping. Non-matching braces stay verbatim.
Order: (1) row and path=='i' → fmt(row.__i) (1-based); (2) row and path startswith 'row.' → fmt(row[path[4:]]) raw; (3) 'values.' → fmt(formatValue(ctx, rest)); (4) 'svc.' → fmt(serviceValue(ctx, rest)); (5) else fmt(getPath(ctx, path)) — dotted traversal, missing → ''.
Interpolation applies ONLY to TextElement.text and showIf. NOT to table cells/staticRows/column bind/header, image src, field labels.

## 3. Elements
### 3.0 Page & frame
PAPER_PX = A4 794×1123, A5 559×794, Letter 816×1056; landscape swaps. Background = doc.background (hex). `doc.margin` is editor-only (not rendered). Single page only, no pagination; everything clipped to the page box. Element frame: left=x, top=y, width=w, height=h, opacity (default 1) applied to whole element, rotation degrees about element centre, z-order = array index (first = bottom). `hidden` → skip. `locked` no effect.

### 3.1 repeat
If `el.repeat`: rows = tableRows(ctx, repeat.fieldKey); for i,row: clone with y = el.y + i*step and row context {...row, __i: i+1}. Works for any element type (row consumed by text interpolation and showIf). {i} 1-based. Zero rows → element disappears. No clipping.

### 3.2 showIf
Per clone: keep iff interpolate(showIf, ctx, row).strip() != ''. Presence test only: '0', 'false', '—' count as present; '' / whitespace / missing → hidden. No operators.

### 3.3 text
`lineH = fontSize * (lineHeight or 1.35)`; `singleLine = el.h < lineH*2 and '\n' not in el.text (RAW template text)`.
Style: fontFamily sans|serif|mono, fontSize px (may be fractional), fontWeight 400/500/600/700, italic, underline, color, align left|center|right|justify, vAlign top|middle|bottom (default top), lineHeight multiplier (default 1.35), letterSpacing px (added after every char incl. last), background fills whole element box, padding (all sides, default 0), overflow hidden.
Fonts: sans → Onest/system sans (backend: DejaVu Sans or similar), serif → **Times New Roman** (Liberation Serif fallback), mono → JetBrains Mono / DejaVu Sans Mono. Must cover Cyrillic + ‘ ’ (U+2018/2019) + ✓ ‼ № ≥ ≤ – —.
Multi-line: pre-wrap (hard breaks on \n, spaces preserved, soft wrap at box width minus padding, break long words). Single-line: one line, clipped at box edge, no ellipsis. Vertical: text block (nLines*lineH) at top/middle/bottom of content box; overflow clipped. Half-leading: first baseline ≈ top + padding + (lineH − fontSize)/2 + ascent. justify: all lines except last of paragraph.
Default style: sans 13px 400 #14201d left top 1.35.

### 3.4 field
def = schema field by el.fieldKey; label = def.label or fieldKey; value = formatValue; unit = def.unit if showUnit; ref = referenceText (numbers only) if showReference; flag = fieldFlag if highlightAbnormal else 'unknown'; abnormal = flag in (abnormal, critical).
Table-typed field → one line: [label + ': ' bold(600) if showLabel] + f"{len(rows)} qator" (literal Uzbek 'qator').
Normal: single ROW, baseline-aligned, gap 8px, from left (align/vAlign ignored). Children: (1) label span if showLabel (flex-grow, wraps); (2) value span: fontWeight 600 always, color #c2413f if abnormal else style color, nowrap, text = value or '—', suffix ' ‼' if critical, ' *' if abnormal; (3) unit span only if unit non-empty: opacity .7 (blend 0.7·color+0.3·white), min-width 60; (4) reference span whenever showReference (even if ''): opacity .7, min-width 90, right-aligned.

### 3.5 rect / ellipse
fill (transparent if undefined), border = strokeWidth(default 1) solid stroke if stroke set, radius px (rect) / 50% (ellipse = ellipse inscribed in w×h). Never dashed. Stroke drawn INSIDE the box (border-box): inset path by strokeWidth/2.

### 3.6 line
horizontal: from (x,y) to (x+w,y), thickness strokeWidth growing DOWN from y (h ignored). vertical: from (x,y) to (x,y+h), thickness growing RIGHT from x (w ignored). dashed → dash 2*sw, gap sw. Colour = stroke.

### 3.7 image
src = el.src or asset(el.assetId).url. fit: contain (aspect, centred, letterbox) | cover (aspect, centred, cropped — clip) | fill (stretch). Missing src → box with 1px dashed #999 border and centred text "image" 10px #999. Data URIs (png/jpg/svg) and stored asset files must be supported; SVG via fpdf2 svg support, raster fallback ok.

### 3.8 table
rows = fieldKey ? tableRows(ctx, fieldKey) : staticRows mapped to dict {col.bind or str(i): r[i] or ''}. cols = TableField.columns of schema field `fieldKey` if it is a table field else [].
Column `bind` = plain key lookup `row[bind]` (never interpolated). For 'items' the keys are i/code/name/status/<field keys> (already strings). Static tables: positional rows keyed by bind (bind '' → empty cell; `{row.x}`-style bind is NOT interpolated).
fmtCell(row, bind): v None/'' → ('', False); col select → (label or str(v), option.flag in abnormal/critical); multiselect list → (labels joined ', ', False); boolean → (trueLabel or '✓' / falseLabel or '—', False); number & numeric → (str(v) [decimals ignored], abnormal by references[0] min/max only); else (', '.join(v) if list else str(v), False).
Geometry: totalW = sum(col.width) or 1; numW = 28 if showRowNumber else 0; avail = el.w − numW; colWidth_k = width_k/totalW * avail. Header row if showHeader: headerStyle, padding 4px 6px, min height rowHeight, text-align col.align; № header centred. Body cells: cellStyle, padding 3px 6px, min height rowHeight (grows if text wraps: pre-wrap, break-word), vertically MIDDLE aligned, text-align col.align; row number i+1 centred. Zebra: background el.zebra on 0-based ODD data rows (2nd, 4th …), never header. Abnormal cell (highlightAbnormal): color #c2413f + weight 600. Borders: border-collapse → single grid of borderWidth lines in borderColor at cell boundaries + outer rectangle; borderWidth 0 → none. Table clipped to el.h (overflow hidden), no pagination.
Editor defaults: header 10px 600 #5c6b66; cell 10.5px #14201d; rowHeight 22; borderColor #c3cec9; borderWidth 1.

## 4. Abnormal evaluation
ABN colour = #c2413f (single colour; critical differs only by ‼ marker on field elements).
fieldFlag: def missing or raw None/'' → unknown; number & numeric → evaluateNumber(...) → normal|unknown|abnormal (low/high → abnormal); select → option.flag or unknown; else unknown. Critical only from select option flag.
evaluateNumber(field, value, {gender, ageMonths}): first ref where (no gender mismatch when both known) and (ageFromMonths/ageToMonths satisfied when ageMonths known) and (min or max set); none → unknown; value < min → low; value > max → high; else normal (inclusive normal).
referenceText(field, {gender}): first ref where !r.gender or !gender or equal (no age filter); none → ''; ref.text if set; both min&max → f"{min} – {max}" (EN DASH, spaces); min only → f"≥ {min}"; max only → f"≤ {max}"; else ''. Numbers via plain str (JS String(number): 12 → "12", 12.5 → "12.5").
fieldUnit = def.unit or ''. Reference is '' for non-number fields.

## 5. Known quirks (decisions for backend)
1. `{values.<tableKey>}` → return '' (JS prints [object Object]).
2. Static-table bind '' → empty; bind expressions not interpolated.
3. date-typed values print raw yyyy-MM-dd.
4. Table numbers ignore decimals; table abnormal uses references[0] only.
5. patient.gender label language: backend uses template.language.
6. '—' vs '' per table in §1.
7. singleLine decided from raw template text (\n) + box height.
8. field elements ignore align/vAlign; value always weight 600.
9. showIf presence-only.
10. multiselect non-array: guard.
11. No multi-page: clip.
