(function () {
  if (document.getElementById("fsnav-root")) return;

  var LINKS = [
    { href: "/", label: "3D view" },
    { href: "/editor.html", label: "Map editor" },
    { href: "/command.html", label: "Commander" },
    { href: "/firefighter.html", label: "Firefighter" },
    { href: "/phone.html", label: "Occupant phone" },
    { href: "/pdr-test.html", label: "Sensor test" },
    { href: "/api/qr/print", label: "QR sheet" },
    { href: "/docs", label: "API docs" }
  ];

  var style = document.createElement("style");
  style.textContent =
    ".fsnav-btn{position:fixed;top:calc(var(--fsnav-top,8px) + env(safe-area-inset-top,0px));left:calc(var(--fsnav-left,8px) + env(safe-area-inset-left,0px));z-index:2147483001;width:40px;height:40px;padding:0;border:0;border-radius:8px;background:rgba(12,14,18,.72);color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}" +
    ".fsnav-btn:focus-visible{outline:2px solid #fff;outline-offset:2px}" +
    ".fsnav-ico{display:block;width:18px;height:2px;background:#fff;border-radius:1px;position:relative}" +
    ".fsnav-ico:before,.fsnav-ico:after{content:\"\";position:absolute;left:0;width:18px;height:2px;background:#fff;border-radius:1px}" +
    ".fsnav-ico:before{top:-6px}.fsnav-ico:after{top:6px}" +
    ".fsnav-open .fsnav-ico{background:transparent}" +
    ".fsnav-open .fsnav-ico:before{top:0;transform:rotate(45deg)}" +
    ".fsnav-open .fsnav-ico:after{top:0;transform:rotate(-45deg)}" +
    ".fsnav-back{position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.45)}" +
    ".fsnav-drawer{position:fixed;top:0;left:0;bottom:0;z-index:2147483002;width:min(280px,86vw);margin:0;padding:calc(56px + env(safe-area-inset-top,0px)) 12px calc(16px + env(safe-area-inset-bottom,0px));background:#14181f;color:#fff;box-shadow:4px 0 24px rgba(0,0,0,.4);overflow:auto;transform:translateX(-100%);transition:transform .18s ease}" +
    ".fsnav-open .fsnav-drawer{transform:translateX(0)}" +
    ".fsnav-link{display:block;padding:12px 14px;margin:2px 0;border-radius:8px;color:#e8eaed;text-decoration:none;font:16px/1.3 system-ui,sans-serif;min-height:44px;box-sizing:border-box}" +
    ".fsnav-link:active,.fsnav-link:hover{background:rgba(255,255,255,.08)}" +
    ".fsnav-current{background:rgba(255,255,255,.14);color:#fff;font-weight:650}";
  document.head.appendChild(style);

  var path = location.pathname.replace(/\\/g, "/");
  var file = path.split("/").pop();
  var here = !file || file === "index.html" ? "/" : "/" + file;

  var root = document.createElement("div");
  root.id = "fsnav-root";

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "fsnav-btn";
  btn.setAttribute("aria-label", "Menu");
  btn.setAttribute("aria-expanded", "false");
  var ico = document.createElement("span");
  ico.className = "fsnav-ico";
  btn.appendChild(ico);

  var back = document.createElement("div");
  back.className = "fsnav-back";
  back.hidden = true;

  var drawer = document.createElement("nav");
  drawer.className = "fsnav-drawer";
  drawer.setAttribute("aria-label", "Pages");

  LINKS.forEach(function (item) {
    var a = document.createElement("a");
    a.className = "fsnav-link" + (item.href === here ? " fsnav-current" : "");
    a.href = item.href;
    a.textContent = item.label;
    if (item.href === here) a.setAttribute("aria-current", "page");
    drawer.appendChild(a);
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
})();
