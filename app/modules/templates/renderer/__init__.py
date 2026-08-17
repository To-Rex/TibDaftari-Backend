"""Result-document renderer — Python port of the frontend DocumentRenderer (docs/RENDERER_SPEC.md).

* `expressions` — pure placeholder / value helpers (interpolate, formatValue, tableRows …)
* `context` — RenderContext builder from plain dicts (patient/order/item/company/branch/category)
* `pdf` — TemplateDoc + RenderContext → PDF bytes (fpdf2, 1 unit = 1 CSS px, pt = px·0.75)
"""

from app.modules.templates.renderer.context import build_render_context, gender_label
from app.modules.templates.renderer.pdf import AssetLoader, render

__all__ = ["AssetLoader", "build_render_context", "gender_label", "render"]
