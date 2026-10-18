"""Client-only responsive adjustments; production decoder files stay unchanged."""

# 雷达页只做响应式布局微调。
STYLE = """<style id="client-radar-layout">
@media(max-width:900px){
  #quick{width:calc(100vw - 24px);max-width:380px}
  #quick .qk{flex:1;min-width:0}
  #playbar{display:grid;grid-template-columns:36px minmax(70px,1fr) auto;
    width:calc(100vw - 24px);max-width:430px;gap:6px}
  #playbar #seek{min-width:0;width:100%}
  #playbar .spd{grid-column:1/-1;justify-content:center}
  body.mode3d #camseg{bottom:calc(var(--sab) + 78px);z-index:670;white-space:nowrap}
  body.mode3d #playbar{bottom:calc(var(--sab) + 122px)}
  body.mode3d #map.mini{bottom:calc(var(--sab) + 216px)}
  body.mode3d #toast{bottom:calc(var(--sab) + 222px)}
}
</style>"""


def adapt_page(page):
    if 'id="client-radar-layout"' in page:
        return page
    return page.replace("</head>", STYLE + "</head>", 1)
