(function () {
  try {
    if (window.top !== window) return;
  } catch (e) {
    return;
  }
  if (document.getElementById("fsnav-root")) return;
  if (!document.body) return;

  var SECTIONS = [
    { title: "View", links: [
      { href: "/", label: "3D view" },
      { href: "/editor.html", label: "Map editor" }
    ]},
    { title: "Respond", links: [
      { href: "/command.html", label: "Commander" },
      { href: "/firefighter.html", label: "Firefighter" }
    ]},
    { title: "Reference", links: [
      { href: "/api/qr/print", label: "QR sheet" },
      { href: "/docs", label: "API docs" }
    ]}
  ];

  var style = document.createElement("style");
  style.textContent = [
    ".fsnav-btn{position:fixed;top:var(--fsnav-top,calc(10px + env(safe-area-inset-top,0px)));right:var(--fsnav-right,calc(12px + env(safe-area-inset-right,0px)));z-index:2147483003;height:34px;padding:0 12px;box-sizing:border-box;border:1px solid rgba(255,255,255,.22);border-radius:999px;background:rgba(14,17,22,.72);color:#fff;font:600 13px/1 system-ui,-apple-system,Segoe UI,sans-serif;letter-spacing:0;white-space:nowrap;cursor:pointer;display:inline-flex;align-items:center;gap:6px;box-shadow:0 4px 18px rgba(0,0,0,.28);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);-webkit-tap-highlight-color:transparent}",
    ".fsnav-btn:hover{background:rgba(40,48,60,.88);border-color:rgba(255,255,255,.42)}",
    ".fsnav-btn:focus-visible{outline:2px solid #fff;outline-offset:2px}",
    ".fsnav-btn:active{transform:none}",
    ".fsnav-back{position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.45)}",
    ".fsnav-drawer{position:fixed;top:0;right:0;bottom:0;z-index:2147483002;width:min(280px,86vw);margin:0;padding:16px 12px calc(16px + env(safe-area-inset-bottom,0px));background:#14181f;color:#fff;box-shadow:-4px 0 24px rgba(0,0,0,.4);overflow:auto;transform:translateX(100%);transition:transform .18s ease}",
    ".fsnav-open .fsnav-drawer{transform:translateX(0)}",
    ".fsnav-sec{margin:14px 6px 2px;padding:0 8px;color:#8b949e;font:600 10px/1.3 system-ui,-apple-system,Segoe UI,sans-serif;letter-spacing:.06em;text-transform:uppercase}",
    ".fsnav-drawer .fsnav-sec:first-child{margin-top:2px}",
    ".fsnav-link{display:block;padding:12px 14px;margin:2px 0;border-radius:8px;color:#e8eaed;text-decoration:none;font:15px/1.3 system-ui,-apple-system,Segoe UI,sans-serif;min-height:44px;box-sizing:border-box}",
    ".fsnav-link:active,.fsnav-link:hover{background:rgba(255,255,255,.08)}",
    ".fsnav-current{background:rgba(255,255,255,.14);color:#fff;font-weight:650}",
    "@media (prefers-reduced-motion:reduce){.fsnav-drawer{transition:none}}"
  ].join("");
  document.head.appendChild(style);

  var rootStyle = getComputedStyle(document.documentElement);
  var bodyStyle = getComputedStyle(document.body);
  var rootTop = rootStyle.getPropertyValue("--fsnav-top").trim();
  var rootRight = rootStyle.getPropertyValue("--fsnav-right").trim();
  var bodyTop = bodyStyle.getPropertyValue("--fsnav-top").trim();
  var bodyRight = bodyStyle.getPropertyValue("--fsnav-right").trim();
  var locked = !!(rootTop || rootRight || (bodyTop && bodyTop !== rootTop) || (bodyRight && bodyRight !== rootRight));

  var path = location.pathname.replace(/\\/g, "/");
  var file = path.split("/").pop();
  var here = !file || file === "index.html" ? "/" : "/" + file;

  var root = document.createElement("div");
  root.id = "fsnav-root";
  root.style.visibility = "hidden";

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "fsnav-btn";
  btn.id = "fsnav-btn";
  btn.setAttribute("aria-label", "Menu");
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-controls", "fsnav-drawer");
  btn.textContent = "\u2630 Menu";

  var back = document.createElement("div");
  back.className = "fsnav-back";
  back.hidden = true;

  var drawer = document.createElement("nav");
  drawer.className = "fsnav-drawer";
  drawer.id = "fsnav-drawer";
  drawer.setAttribute("aria-label", "Pages");

  SECTIONS.forEach(function (sec) {
    var h = document.createElement("div");
    h.className = "fsnav-sec";
    h.textContent = sec.title;
    drawer.appendChild(h);
    sec.links.forEach(function (item) {
      var a = document.createElement("a");
      a.className = "fsnav-link" + (item.href === here ? " fsnav-current" : "");
      a.href = item.href;
      a.textContent = item.label;
      if (item.href === here) a.setAttribute("aria-current", "page");
      drawer.appendChild(a);
    });
  });

  function setOpen(open) {
    root.classList.toggle("fsnav-open", open);
    back.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? "Close menu" : "Menu");
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    setOpen(!root.classList.contains("fsnav-open"));
  });
  back.addEventListener("click", function () { setOpen(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setOpen(false);
  });

  root.appendChild(back);
  root.appendChild(drawer);
  root.appendChild(btn);
  document.body.appendChild(root);

  function clearPad() {
    var row = document.querySelector(".wrap > .row[data-fsnav-pad]");
    if (!row) return;
    row.style.paddingRight = "";
    row.removeAttribute("data-fsnav-pad");
  }

  function overlaps(s) {
    var w = Math.ceil(btn.offsetWidth || 96);
    var h = 34;
    var left = window.innerWidth - s.right - w;
    var top = s.top;
    if (left < 4 || top < 4 || left + w > window.innerWidth - 2) return true;
    var nodes = document.querySelectorAll("button,a,input,select,textarea,header,#panel,#hint,#toast,#zoombar,#link,.dock,#victimAlert,label");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.closest("#fsnav-root")) continue;
      if (el.hidden) continue;
      var st = getComputedStyle(el);
      if (st.display === "none" || st.visibility === "hidden") continue;
      var b = el.getBoundingClientRect();
      if (b.width < 2 || b.height < 2) continue;
      var gap = 6;
      var clear = left > b.right + gap || left + w < b.left - gap || top > b.bottom + gap || top + h < b.top - gap;
      if (!clear) return true;
    }
    return false;
  }

  function defaultSpot() {
    return { top: 10, right: 12 };
  }

  function belowPanel() {
    var panel = document.getElementById("panel");
    if (!panel) return null;
    var b = panel.getBoundingClientRect();
    if (b.width < 2 || b.height < 2) return null;
    return { top: Math.round(b.bottom + 8), right: 12 };
  }

  function belowHeader() {
    var header = document.querySelector("header");
    if (!header) return null;
    var b = header.getBoundingClientRect();
    if (b.width < 2 || b.height < 2) return null;
    var top = b.bottom + 8;
    var alert = document.getElementById("victimAlert");
    if (alert && !alert.hidden) {
      var a = alert.getBoundingClientRect();
      if (a.height > 2) top = a.bottom + 8;
    }
    return { top: Math.round(top), right: 12 };
  }

  function stageSpot() {
    var stage = document.getElementById("stage");
    if (!stage) return null;
    var b = stage.getBoundingClientRect();
    if (b.width < 160 || b.height < 80) return null;
    return {
      top: Math.round(b.top + 10),
      right: Math.round(window.innerWidth - b.right + 10)
    };
  }

  function firefighterSpot() {
    var row = document.querySelector(".wrap > .row");
    var link = document.getElementById("link");
    if (!row || !link) return null;
    var w = Math.ceil(btn.offsetWidth || 96);
    var pad = w + 22;
    var spot = null;
    for (var n = 0; n < 3; n++) {
      row.style.paddingRight = pad + "px";
      row.setAttribute("data-fsnav-pad", "1");
      var rb = row.getBoundingClientRect();
      spot = { top: Math.round(rb.top + Math.max(0, (rb.height - 34) / 2)), right: 12 };
      var lr = link.getBoundingClientRect();
      var left = window.innerWidth - spot.right - w;
      if (lr.right + 8 <= left && spot.top >= 4) return spot;
      pad += Math.max(8, Math.ceil(lr.right + 8 - left));
      spot = null;
    }
    return spot;
  }

  function applySpot(spot) {
    if (spot.top === 10 && spot.right === 12) {
      document.documentElement.style.removeProperty("--fsnav-top");
      document.documentElement.style.removeProperty("--fsnav-right");
      return;
    }
    document.documentElement.style.setProperty("--fsnav-top", spot.top + "px");
    document.documentElement.style.setProperty("--fsnav-right", spot.right + "px");
  }

  function syncDrawer() {
    var b = btn.getBoundingClientRect();
    var drawerW = Math.min(280, window.innerWidth * 0.86);
    if (b.width > 2 && b.right > window.innerWidth - drawerW + 4) {
      drawer.style.paddingTop = Math.round(b.bottom + 12) + "px";
    } else {
      drawer.style.paddingTop = "";
    }
  }

  function place() {
    if (!locked) {
      clearPad();
      var options = [defaultSpot, belowPanel, belowHeader, stageSpot, firefighterSpot];
      var spot = null;
      for (var i = 0; i < options.length; i++) {
        var s = options[i]();
        if (s && !overlaps(s)) { spot = s; break; }
      }
      if (!spot) {
        clearPad();
        spot = defaultSpot();
      }
      applySpot(spot);
    }
    syncDrawer();
    root.style.visibility = "";
  }

  place();
  var resizeTick = 0;
  window.addEventListener("resize", function () {
    if (resizeTick) cancelAnimationFrame(resizeTick);
    resizeTick = requestAnimationFrame(function () {
      resizeTick = 0;
      place();
    });
  });
})();
