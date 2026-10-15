/* Chomp dashboard: small enhancements on top of plain forms, which all work without this
   file. Sliders mirror their number boxes, quick-pick chips fill them in, and previews show
   what a change will do before it is saved. The preview math repeats logic.scale on the
   numbers the page was rendered with; what gets saved is always computed by the server. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  var PLACES = { kcal: 0, protein_g: 1, carbs_g: 1, fat_g: 1 };
  var KCAL_PER_G = { protein_g: 4, carbs_g: 4, fat_g: 9 };

  function parse(value) {
    var n = parseFloat(String(value).replace(/,/g, ""));
    return isFinite(n) ? n : null;
  }

  function fmt(value, places) {
    return value.toLocaleString("en-US", { maximumFractionDigits: places });
  }

  function json(el, name) {
    try {
      return el.dataset[name] ? JSON.parse(el.dataset[name]) : null;
    } catch (err) {
      return null;
    }
  }

  // The grams a food form will log: its servings times the serving size when Servings is
  // picked (#80), else its grams box
  function amountGrams(form) {
    var by = form.querySelector("input[name=by]:checked");
    if (by && by.value === "servings") {
      var box = form.querySelector("[data-servings]");
      var n = parse(box.value);
      return n !== null ? n * parse(box.closest("[data-serving]").dataset.serving) : null;
    }
    var field = form.querySelector("[data-amount]");
    return field ? parse(field.value) : null;
  }

  // Food previews: "= 214 kcal · P 18.9 C 1.1 F 14.3", and the day's calories after saving
  function foodPreview(form) {
    var per = json(form, "per100");
    var out = form.querySelector(".preview");
    if (!per || !out || !form.querySelector("[data-amount]")) return;
    var grams = amountGrams(form);
    var ok = grams !== null && grams > 0;
    Object.keys(PLACES).forEach(function (k) {
      var el = out.querySelector('[data-n="' + k + '"]');
      if (el) el.textContent = ok ? fmt((per[k] * grams) / 100, PLACES[k]) : "–";
    });
    var day = out.querySelector("[data-day]");
    if (day) {
      var base = parse(form.dataset.base) || 0;
      var goal = parse(form.dataset.goal);
      var total = base + (ok ? (per.kcal * grams) / 100 : 0);
      day.textContent = fmt(total, 0) + (goal ? " of " + fmt(goal, 0) : "") + " kcal";
      day.classList.toggle("warn", !!goal && total > goal);
    }
  }

  // Goals: one calorie bar split into protein, carbs and fat. Dragging a border trades
  // calories between the two slices beside it while the total stays the number typed above
  // the bar; a new number starts the slices at even thirds. Grams move in 1 g steps and no
  // slice gets under 5%. The math repeats logic.fit_macros; the server checks what's saved.
  var MACRO_KEYS = ["protein_g", "carbs_g", "fat_g"];
  var MIN_SHARE = 0.05;

  function calorieBar(form) {
    var bar = form.querySelector("[data-calbar]");
    if (!bar) return;
    var kcalField = form.elements.kcal;
    var handles = bar.querySelectorAll("[data-handle]");
    var grams = {};
    MACRO_KEYS.forEach(function (k) {
      grams[k] = parse(form.elements[k].value) || 0;
    });

    function total() {
      var n = parse(kcalField.value);
      return n !== null && n > 0 ? n : null;
    }

    function clamp(n, lo, hi) {
      return Math.max(lo, Math.min(hi, n));
    }

    // Carbs take whatever protein and fat leave
    function settle(kcal) {
      var rest = kcal - grams.protein_g * 4 - grams.fat_g * 9;
      grams.carbs_g = Math.max(Math.round(rest / 4), 0);
    }

    function thirds(kcal) {
      grams.protein_g = Math.round(kcal / 3 / 4);
      grams.fat_g = Math.round(kcal / 3 / 9);
      settle(kcal);
    }

    // which 0 is the protein | carbs border (sets protein), 1 the carbs | fat one (sets fat)
    function setBorder(which, g) {
      var kcal = total();
      if (!kcal) return;
      var key = which === 0 ? "protein_g" : "fat_g";
      var other = which === 0 ? "fat_g" : "protein_g";
      var per = KCAL_PER_G[key];
      var lo = Math.ceil((MIN_SHARE * kcal) / per);
      var hi = Math.floor(((1 - MIN_SHARE) * kcal - grams[other] * KCAL_PER_G[other]) / per);
      grams[key] = clamp(Math.round(g), lo, hi);
      settle(kcal);
      draw();
    }

    function draw() {
      var parts = {};
      var sum = 0;
      MACRO_KEYS.forEach(function (k) {
        parts[k] = grams[k] * KCAL_PER_G[k];
        sum += parts[k];
      });
      if (sum <= 0) return;
      var x = 0;
      var out = false;
      MACRO_KEYS.forEach(function (k, i) {
        var share = parts[k] / sum;
        var rect = bar.querySelector('rect[data-k="' + k + '"]');
        rect.setAttribute("x", (100 * x).toFixed(2));
        rect.setAttribute("width", (100 * share).toFixed(2));
        var lab = bar.querySelector('[data-lab="' + k + '"]');
        lab.style.left = 100 * x + "%";
        lab.style.width = 100 * share + "%";
        lab.querySelector("[data-g]").textContent = fmt(grams[k], 1);
        lab.querySelector("[data-pct]").textContent = Math.round(100 * share);
        lab.classList.remove("out");
        if (lab.querySelector(".txt").offsetWidth > lab.clientWidth - 8) {
          lab.classList.add("out");
          out = true;
        }
        form.elements[k].value = String(grams[k]);
        x += share;
        if (i < 2) {
          var h = handles[i];
          h.style.left = 100 * x + "%";
          var g = grams[i === 0 ? "protein_g" : "fat_g"];
          h.setAttribute("aria-valuenow", String(g));
          h.setAttribute("aria-valuetext", fmt(g, 1) + " grams of " + (i === 0 ? "protein" : "fat"));
        }
      });
      bar.classList.toggle("has-out", out);
    }

    handles.forEach(function (h) {
      var which = parseInt(h.dataset.handle, 10);
      function follow(event) {
        var kcal = total();
        if (!kcal) return;
        var r = bar.getBoundingClientRect();
        var pos = clamp((event.clientX - r.left) / r.width, 0, 1);
        setBorder(which, which === 0 ? (pos * kcal) / 4 : ((1 - pos) * kcal) / 9);
      }
      h.addEventListener("pointerdown", function (event) {
        h.setPointerCapture(event.pointerId);
        h.classList.add("drag");
        event.preventDefault();
      });
      h.addEventListener("pointermove", function (event) {
        if (h.hasPointerCapture(event.pointerId)) follow(event);
      });
      ["pointerup", "pointercancel"].forEach(function (type) {
        h.addEventListener(type, function () {
          h.classList.remove("drag");
        });
      });
      // Arrow keys move the border 1 g at a time
      h.addEventListener("keydown", function (event) {
        var step = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        if (which === 0) setBorder(0, grams.protein_g + step);
        else setBorder(1, grams.fat_g - step);
      });
    });

    kcalField.addEventListener("input", function () {
      var kcal = total();
      if (!kcal) return;
      thirds(kcal);
      var note = form.querySelector("[data-refit]");
      if (note) note.hidden = true;
      draw();
    });
    window.addEventListener("resize", draw);
    draw();
  }

  document.querySelectorAll("form.goals").forEach(calorieBar);

  function changed(form) {
    if (!form) return;
    foodPreview(form);
  }

  // A slider and its number box move together
  document.querySelectorAll(".amount").forEach(function (box) {
    var range = box.querySelector("input[type=range]");
    var field = box.querySelector("[data-amount]");
    if (!range || !field) return;
    // Grams chosen by hand are exact; an estimated portion chip marks them again (#75)
    var estimated = field.form && field.form.querySelector("input[name=estimated]");
    range.addEventListener("input", function () {
      field.value = range.value;
      if (estimated) estimated.value = "0";
      syncServings(field.form, parse(range.value));
      changed(field.form);
    });
    field.addEventListener("input", function () {
      if (estimated) estimated.value = "0";
      var n = parse(field.value);
      if (n !== null) {
        if (n > parse(range.max)) range.max = String(Math.ceil(n));
        range.value = String(n);
      }
      syncServings(field.form, n);
      changed(field.form);
    });
  });

  // Servings (#80): the servings box and the grams box show the same amount, whichever is
  // changed; the Servings / Grams toggle says which one gets logged
  function countText(n) {
    return String(Math.round(n * 100) / 100);
  }
  function syncServings(form, grams) {
    var box = form && form.querySelector("[data-servings]");
    if (!box || grams === null || grams <= 0) return;
    box.value = countText(grams / parse(box.closest("[data-serving]").dataset.serving));
  }
  document.querySelectorAll("[data-servings]").forEach(function (box) {
    var form = box.form;
    var size = parse(box.closest("[data-serving]").dataset.serving);
    var field = form.querySelector("[data-amount]");
    var range = field.closest(".amount").querySelector("input[type=range]");
    var estimated = form.querySelector("input[name=estimated]");
    box.addEventListener("input", function () {
      if (estimated) estimated.value = "0";
      var n = parse(box.value);
      if (n !== null && n > 0) {
        var grams = Math.round(n * size * 10) / 10;
        field.value = String(grams);
        if (range) {
          if (grams > parse(range.max)) range.max = String(Math.ceil(grams));
          range.value = String(grams);
        }
      }
      changed(form);
    });
    // − and + move to the next whole serving down or up (1.5 goes to 1 or 2), not under 1;
    // a typed 1.5 still logs as typed
    box.closest(".field").querySelectorAll("[data-step]").forEach(function (button) {
      button.addEventListener("click", function () {
        var n = parse(box.value) || 0;
        var next = button.dataset.step === "1"
          ? Math.floor(n + 1e-9) + 1
          : Math.ceil(n - 1e-9) - 1;
        if (next < 1) return;
        box.value = countText(next);
        box.dispatchEvent(new Event("input"));
      });
    });
  });
  document.addEventListener("change", function (event) {
    if (event.target.name === "by") changed(event.target.form);
  });

  // Quick picks: "1 serving · 46 g", "1 handful · ~30 g", "100 g"
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-set]");
    if (!chip) return;
    var field = chip.closest(".goal, form").querySelector("[data-amount]");
    field.value = chip.dataset.set;
    // Chips are weights, so a tap on one while Servings is picked switches to Grams
    var grams = field.form && field.form.querySelector("input[name=by][value=grams]");
    if (grams) grams.checked = true;
    field.dispatchEvent(new Event("input"));
    var estimated = field.form && field.form.querySelector("input[name=estimated]");
    if (estimated && chip.dataset.estimated) estimated.value = "1";
  });

  // Time nudges on meal times: -15 min, +15 min, now
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-nudge]");
    if (!chip) return;
    var field = chip.closest("form").querySelector("input[type=time]");
    var step = chip.dataset.nudge;
    var minutes;
    if (step === "now") {
      var now = new Date();
      minutes = now.getHours() * 60 + now.getMinutes();
    } else {
      var parts = (field.value || "12:00").split(":");
      minutes = (parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + parseInt(step, 10)
        + 1440) % 1440;
    }
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    field.value = pad(Math.floor(minutes / 60)) + ":" + pad(minutes % 60);
  });

  // Close a confirmation banner
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dismiss]");
    if (button) button.closest(".flash").remove();
  });

  // "Move to" and "Add to" menus: the time box only matters for a new meal
  document.querySelectorAll("select[data-new-meal]").forEach(function (select) {
    var time = select.form.querySelector(".new-meal");
    var sync = function () {
      if (time) time.hidden = select.value !== "new";
    };
    select.addEventListener("change", sync);
    sync();
  });

  // New food from a label photo: count the ticked values and say whether saving now gives
  // a confirmed food (every filled-in value ticked) or an unconfirmed one
  document.querySelectorAll("form[data-ticks]").forEach(function (form) {
    var count = form.querySelector("[data-tick-count]");
    var save = form.querySelector("[data-save]");
    var sync = function () {
      var filled = 0;
      var ticked = 0;
      form.querySelectorAll(".vrow").forEach(function (row) {
        var box = row.querySelector("input[type=number]");
        var tick = row.querySelector("input[type=checkbox]");
        if (!box || !tick || box.value.trim() === "") return;
        filled += 1;
        if (tick.checked) ticked += 1;
      });
      var all = filled > 0 && ticked === filled;
      count.textContent = ticked + " of " + filled + " values ticked" +
        (all ? ". Saves as confirmed." : ". Tick every value to save it as confirmed.");
      save.textContent = all ? "Save as confirmed" : "Save as unconfirmed";
    };
    form.addEventListener("input", sync);
    form.addEventListener("change", sync);
    sync();
  });

  // Foods and Breakdown: remember which cards were folded (1) or opened (0), in this
  // browser only
  var folded = {};
  try {
    folded = JSON.parse(localStorage.getItem("chomp-folded") || "{}") || {};
  } catch (err) {
    folded = {};
  }
  function foldable(fold) {
    var name = fold.dataset.fold;
    if (folded[name] === 1) fold.open = false;
    else if (folded[name] === 0) fold.open = true;
    fold.addEventListener("toggle", function () {
      folded[name] = fold.open ? 0 : 1;
      try {
        localStorage.setItem("chomp-folded", JSON.stringify(folded));
      } catch (err) {
        // private browsing: folding still works, it just isn't remembered
      }
    });
  }
  document.querySelectorAll("details[data-fold]").forEach(foldable);

  // Breakdown: a link to a section (tapping Protein on Today) opens the card it's in
  var reveal = function () {
    var target = location.hash && document.getElementById(location.hash.slice(1));
    if (!target) return;
    var fold = target.closest("details[data-fold]");
    if (fold && !fold.open) fold.open = true;
    target.scrollIntoView();
  };
  reveal();
  window.addEventListener("hashchange", reveal);

  // Foods (#72): a tap opens a food's page. Swipe a food left to show its Delete button,
  // or an unconfirmed one right to show Confirm; swipe back, tap it or swipe another to hide
  // it again. Only one shows at a time. Press and hold a food for a menu (Edit, Rename,
  // Confirm, Log it, Delete) over the blurred page. Portions on a food's page swipe left to
  // delete and open with a press and hold to edit.
  var OPEN = 96;
  var HOLD_MS = 450;
  var shown = null;
  function slide(body, x) {
    body.style.transform = x ? "translateX(" + x + "px)" : "";
    body.dataset.x = String(x || 0);
  }
  function hide() {
    if (shown) slide(shown, 0);
    shown = null;
  }
  document.addEventListener("pointerdown", function (event) {
    if (shown && !shown.parentNode.contains(event.target)) hide();
  });
  function swipeable(body) {
    var li = body.parentNode;
    var left = null; // Confirm, behind the food's left edge
    var right = null; // Delete, behind its right edge
    Array.prototype.forEach.call(li.children, function (child) {
      if (!child.classList.contains("swipe-action")) return;
      if (child.classList.contains("confirm")) left = child;
      else right = child;
    });
    var details = body.querySelector(":scope > details");
    var summary = details && details.querySelector("summary");
    var press = summary || body.querySelector(".food-link");
    var start = null;
    var dx = 0;
    var sliding = false;
    var timer = null;
    var held = false;
    function unhold() {
      clearTimeout(timer);
      timer = null;
      press.classList.remove("holding");
    }
    function onHold() {
      unhold();
      held = true;
      if (navigator.vibrate) navigator.vibrate(15);
      if (details) details.open = true;
      else if (li.dataset.hold) location.href = li.dataset.hold; // a recipe opens to edit
      else holdMenu(li);
    }
    body.addEventListener("pointerdown", function (event) {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      // A portion inside an open food swipes and holds on its own, not the food with it
      if (event.target.closest("[data-swipe]") !== body) return;
      start = { x: event.clientX, y: event.clientY, base: parseFloat(body.dataset.x) || 0 };
      dx = start.base;
      sliding = false;
      held = false;
      unhold();
      if (body !== shown && !(details && details.open) && press.contains(event.target)) {
        press.classList.add("holding");
        timer = setTimeout(onHold, HOLD_MS);
      }
    });
    // A long press shouldn't select text or pop up the phone's menu; a right click on a
    // computer opens the hold menu
    press.addEventListener("contextmenu", function (event) {
      if (timer || held) {
        event.preventDefault();
        return;
      }
      if (!details && event.pointerType !== "touch") {
        event.preventDefault();
        hide();
        if (li.dataset.hold) location.href = li.dataset.hold;
        else holdMenu(li);
      }
    });
    body.addEventListener("pointermove", function (event) {
      if (!start) return;
      var mx = event.clientX - start.x;
      var my = event.clientY - start.y;
      if (Math.abs(mx) > 8 || Math.abs(my) > 8) unhold();
      if (!sliding) {
        if (Math.abs(mx) < 8 || Math.abs(mx) < Math.abs(my)) {
          if (Math.abs(my) > 8) start = null; // a scroll, not a swipe
          return;
        }
        sliding = true;
        if (shown && shown !== body) hide();
        body.classList.add("dragging");
        try {
          body.setPointerCapture(event.pointerId);
        } catch (err) {
          // capture is a nicety
        }
      }
      var lo = right ? -OPEN - 24 : 0;
      var hi = left ? OPEN + 24 : 0;
      dx = Math.max(lo, Math.min(hi, start.base + mx));
      slide(body, dx);
    });
    function end() {
      unhold();
      if (!start) return;
      body.classList.remove("dragging");
      if (sliding) {
        var to = dx < -OPEN / 2 ? -OPEN : dx > OPEN / 2 ? OPEN : 0;
        slide(body, to);
        if (to) shown = body;
        else if (shown === body) shown = null;
      }
      start = null;
    }
    body.addEventListener("pointerup", end);
    body.addEventListener("pointercancel", end);
    // The tap that ends a swipe or a hold, or a tap on a food showing an action, doesn't
    // open it. A portion opens only with a press and hold, though Enter still opens it.
    body.addEventListener("click", function (event) {
      if (sliding || body === shown) {
        event.preventDefault();
        event.stopPropagation();
        if (!sliding) hide();
        sliding = false;
        held = false;
        return;
      }
      if (!press.contains(event.target)) return;
      if (held || (summary && !details.open && event.detail > 0)) event.preventDefault();
      held = false;
    }, true);
    // Tabbing onto Delete or Confirm slides the food aside so the button is seen first
    [[right, -OPEN], [left, OPEN]].forEach(function (pair) {
      var action = pair[0];
      if (!action) return;
      action.addEventListener("focusin", function () {
        if (shown && shown !== body) hide();
        slide(body, pair[1]);
        shown = body;
      });
      action.addEventListener("focusout", function () {
        if (shown === body) hide();
      });
    });
  }
  document.querySelectorAll("[data-swipe]").forEach(swipeable);

  // The hold menu: the food lifts out of the list, a little larger, over the blurred and
  // dimmed page, with its actions underneath (above it near the bottom of the screen).
  // A tap outside, Escape or the back button closes it.
  function holdMenu(li) {
    var url = li.dataset.food;
    var name = li.dataset.name;
    var back = li.dataset.back;
    var body = li.querySelector("[data-swipe]");
    var rect = body.getBoundingClientRect();
    var layer = document.createElement("div");
    layer.className = "hold-layer";
    var lifted = body.cloneNode(true);
    lifted.removeAttribute("data-swipe");
    lifted.removeAttribute("style");
    lifted.className = "swipe-body lifted" + (li.classList.contains("unconfirmed") ? " unconfirmed" : "");
    lifted.style.top = rect.top + "px";
    lifted.style.left = rect.left + "px";
    lifted.style.width = rect.width + "px";
    var menu = document.createElement("div");
    menu.className = "hold-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", name);

    function post(action, fields) {
      var form = document.createElement("form");
      form.method = "post";
      form.action = url + action;
      Object.keys(fields).forEach(function (k) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = k;
        input.value = fields[k];
        form.appendChild(input);
      });
      document.body.appendChild(form);
      form.submit();
    }
    function item(label, run, cls, stay) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "item" + (cls ? " " + cls : "");
      b.setAttribute("role", "menuitem");
      b.textContent = label;
      b.addEventListener("click", function (event) {
        if (!armed && event.detail !== 0) return;
        if (stay) run();
        else leave(run);
      });
      menu.appendChild(b);
      return b;
    }
    // Drop the menu's own history entry before acting, so Back after an action goes where
    // it would have without the menu
    function leave(run) {
      if (!(history.state && history.state.holdMenu)) return run();
      window.removeEventListener("popstate", onBack);
      window.addEventListener("popstate", function once() {
        window.removeEventListener("popstate", once);
        run();
      });
      history.back();
    }
    function go(href) {
      return function () {
        location.href = href;
      };
    }
    item("Edit", go(url + "/edit?back=" + encodeURIComponent(back)));
    item("Rename", rename, "", true);
    var hasPhoto = li.dataset.photo === "1";
    // The file picker has to open from the tap itself, so the menu stays until a photo is
    // chosen (#74)
    item(hasPhoto ? "Change photo" : "Add photo", choosePhoto, "", true);
    if (hasPhoto) {
      item("Remove photo", function () {
        post("/photo/delete", { back: back });
      });
    }
    if (li.dataset.confirmed !== "1") {
      item("Confirm", function () {
        post("/confirm", { back: back, confirmed: "1" });
      }, "ok");
    }
    if (li.dataset.log) item("Log it", go(li.dataset.log));
    item("Delete", function () {
      post("/delete", { back: back });
    }, "danger");

    function choosePhoto() {
      var form = document.createElement("form");
      form.method = "post";
      form.action = url + "/photo";
      form.enctype = "multipart/form-data";
      form.hidden = true;
      var keep = document.createElement("input");
      keep.type = "hidden";
      keep.name = "back";
      keep.value = back;
      var file = document.createElement("input");
      file.type = "file";
      file.name = "photo";
      file.accept = "image/*";
      file.addEventListener("change", function () {
        if (!file.files.length) return;
        leave(function () {
          form.submit();
        });
      });
      form.append(keep, file);
      document.body.appendChild(form);
      file.click();
    }

    // Rename in place: the menu turns into the name box
    function rename() {
      menu.textContent = "";
      menu.classList.add("renaming");
      var form = document.createElement("form");
      form.method = "post";
      form.action = url + "/rename";
      form.className = "stack";
      var keep = document.createElement("input");
      keep.type = "hidden";
      keep.name = "back";
      keep.value = back;
      var label = document.createElement("label");
      label.textContent = "Name";
      var box = document.createElement("input");
      box.name = "name";
      box.value = name;
      box.required = true;
      box.maxLength = 120;
      label.appendChild(box);
      var save = document.createElement("button");
      save.type = "submit";
      save.textContent = "Save name";
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        leave(function () {
          form.submit();
        });
      });
      form.append(keep, label, save);
      menu.appendChild(form);
      place();
      box.focus();
      box.select();
    }

    function place() {
      var gap = 10;
      var margin = 12;
      var h = menu.offsetHeight;
      var top = rect.top;
      var below = top + rect.height + gap + h <= innerHeight - margin;
      if (!below && top - gap - h < margin) {
        // No room either side: move the food up so the menu fits under it
        top = Math.max(margin, innerHeight - margin - h - gap - rect.height);
        below = true;
      }
      lifted.style.top = top + "px";
      menu.style.left = rect.left - 6 + "px"; // under the lifted food's rounded edge
      menu.style.width = Math.min(rect.width, 280) + "px";
      menu.style.top = (below ? top + rect.height + gap : top - gap - h) + "px";
      menu.classList.toggle("above", !below);
    }

    function close() {
      layer.classList.remove("on");
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("popstate", onBack);
      li.classList.remove("held");
      setTimeout(function () {
        layer.remove();
      }, 160);
    }
    function onKey(event) {
      if (event.key === "Escape") {
        close();
        if (history.state && history.state.holdMenu) history.back();
      }
    }
    function onBack() {
      close();
    }
    // The finger that held the food is still down when the menu opens: only a new touch
    // (or a key) picks something, so lifting it can't
    var armed = false;
    layer.addEventListener("pointerdown", function () {
      armed = true;
    });
    layer.addEventListener("click", function (event) {
      if (armed && event.target === layer) {
        close();
        if (history.state && history.state.holdMenu) history.back();
      }
    });
    lifted.addEventListener("click", function (event) {
      var link = event.target.closest("a");
      if (!link) return;
      event.preventDefault();
      if (armed) leave(go(link.href));
    });
    layer.append(lifted, menu);
    document.body.appendChild(layer);
    li.classList.add("held");
    place();
    // Let the browser draw the start state, then lift
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        layer.classList.add("on");
      });
    });
    try {
      history.pushState({ holdMenu: true }, "");
      window.addEventListener("popstate", onBack);
    } catch (err) {
      // the back button just leaves the page then
    }
    document.addEventListener("keydown", onKey);
    menu.querySelector("button").focus({ preventScroll: true });
  }

  // A food's photo (#74): choosing a picture sends it, no Save button needed
  document.querySelectorAll("input[type=file][data-autosubmit]").forEach(function (file) {
    file.addEventListener("change", function () {
      if (!file.files.length) return;
      file.closest("label").classList.add("busy");
      file.form.submit();
    });
  });

  // Foods (#91): picking per 100 g / per serving or a sort shows it, no Go button needed
  function autosubmit(select) {
    select.addEventListener("change", function () {
      select.form.submit();
    });
  }
  document.querySelectorAll("select[data-autosubmit]").forEach(autosubmit);

  // Foods (#97): results follow what's typed, a moment after typing stops, without
  // reloading the page. Only your saved foods are searched; USDA and Open Food Facts
  // wait for a tap on "Search databases". An older answer never replaces a newer one.
  var live = document.querySelector("form[data-live-search]");
  var results = document.querySelector("[data-results]");
  if (live && results && window.fetch) {
    var box = live.querySelector("input[name=q]");
    var clear = live.querySelector("[data-clear]");
    var timer = null;
    var asked = 0;
    var pending = null;
    var current = box.value.trim();
    // `typing`: the last word may be half typed ("chob" finds Chobani)
    function show(q, typing) {
      var mine = ++asked;
      if (pending) pending.abort();
      pending = window.AbortController ? new AbortController() : null;
      var url = live.action + (q ? "?" + new URLSearchParams({ q: q }).toString() : "");
      fetch(url + (q ? "&" : "?") + "part=1" + (typing && q ? "&typing=1" : ""), {
        credentials: "same-origin",
        signal: pending ? pending.signal : undefined,
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      }).then(function (html) {
        if (mine !== asked) return;
        pending = null;
        results.innerHTML = html;
        results.querySelectorAll("details[data-fold]").forEach(foldable);
        results.querySelectorAll("[data-swipe]").forEach(swipeable);
        results.querySelectorAll("select[data-autosubmit]").forEach(autosubmit);
        if (clear) clear.hidden = !q;
        try {
          history.replaceState(history.state, "", url);
        } catch (err) {
          // the address just keeps the old search
        }
      }).catch(function (err) {
        if (err && err.name === "AbortError") return;
        // Offline or signed out: the Search button still works
      });
    }
    box.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = box.value.trim();
        if (q === current) return;
        current = q;
        show(q, true);
      }, 250);
    });
    live.addEventListener("submit", function (event) {
      event.preventDefault();
      clearTimeout(timer);
      current = box.value.trim();
      show(current);
    });
    if (clear) {
      clear.addEventListener("click", function (event) {
        event.preventDefault();
        clearTimeout(timer);
        box.value = "";
        current = "";
        show("");
        box.focus();
      });
    }
  }

  // The month view (#71): tapping the day's title opens it as a sheet over the page. Month
  // arrows and the year list load the next month into the sheet; a day opens that day.
  // A tap outside, Escape or the back button closes it. Without this, the title opens
  // the month view as its own page.
  var opener = document.querySelector("[data-calendar]");
  if (opener && window.fetch) {
    opener.addEventListener("click", function (event) {
      event.preventDefault();
      openCalendar(opener.href);
    });
  }
  function openCalendar(href) {
    var layer = document.createElement("div");
    layer.className = "cal-layer";
    var sheet = document.createElement("div");
    sheet.className = "cal-sheet";
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.setAttribute("aria-label", "Calendar");
    var grab = document.createElement("div");
    grab.className = "grab";
    var body = document.createElement("div");
    sheet.append(grab, body);
    layer.appendChild(sheet);

    function load(url, focus) {
      return fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "part=1", {
        credentials: "same-origin",
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      }).then(function (html) {
        body.innerHTML = html;
        var target = focus && body.querySelector(focus);
        (target || body.querySelector("[aria-current=date], .caldays a, a")).focus({
          preventScroll: true,
        });
      });
    }
    body.addEventListener("click", function (event) {
      var link = event.target.closest("a[href]");
      if (!link) return;
      event.preventDefault();
      if (!link.hasAttribute("data-cal")) {
        // A day (or Today): take the sheet's history entry's place, so Back returns to the
        // page the sheet opened over
        if (history.state && history.state.calendar) location.replace(link.href);
        else location.href = link.href;
        return;
      }
      var label = link.getAttribute("aria-label");
      load(link.href, label ? '[aria-label="' + label + '"]' : null).catch(function () {
        location.href = link.href;
      });
    });

    function close() {
      layer.classList.remove("on");
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("popstate", close);
      setTimeout(function () {
        layer.remove();
      }, 220);
      opener.focus({ preventScroll: true });
    }
    function dismiss() {
      if (history.state && history.state.calendar) history.back();
      else close();
    }
    function onKey(event) {
      if (event.key === "Escape") {
        var years = body.querySelector(".years[open]");
        if (years) years.open = false;
        else dismiss();
      }
    }
    layer.addEventListener("click", function (event) {
      if (event.target === layer) dismiss();
    });

    load(href).then(function () {
      document.body.appendChild(layer);
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          layer.classList.add("on");
        });
      });
      try {
        history.pushState({ calendar: true }, "");
        window.addEventListener("popstate", close);
      } catch (err) {
        // the back button just leaves the page then
      }
      document.addEventListener("keydown", onKey);
    }).catch(function () {
      location.href = href;
    });
  }

  // In the saved meal editor (#60), switching an item between servings and grams keeps
  // its amount: 2 servings of 40 g become 80 g
  document.addEventListener("change", function (event) {
    var unit = event.target;
    if (unit.name !== "unit" || !unit.dataset.serving) return;
    var box = unit.parentNode.querySelector('input[name="amount"]');
    if (!box) return;
    var size = parseFloat(unit.dataset.serving);
    var value = parseFloat(box.value.replace(/,/g, ""));
    if (!size || !isFinite(value)) return;
    var next = unit.value === "g" ? value * size : value / size;
    box.value = String(Math.round(next * 100) / 100);
  });

  // A saved meal's "Today after this" (#60) only shows while the new meal is on today
  document.querySelectorAll("[data-today]").forEach(function (note) {
    var date = note.closest("form").querySelector('input[name="date"]');
    if (!date) return;
    function sync() {
      note.hidden = date.value !== note.dataset.today;
    }
    date.addEventListener("input", sync);
    date.addEventListener("change", sync);
  });

  // Deleting a saved meal (#60) asks first
  document.addEventListener("submit", function (event) {
    var ask = event.target.getAttribute("data-confirm");
    if (ask && !window.confirm(ask)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      hide();
    }
  }, true);

  // The recipe editor (#82) and the saved meal editor (#60): searching for, adding or removing an ingredient asks the
  // server for the form again and swaps it in place, so the page doesn't jump
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var by = event.submitter;
    if (!form.hasAttribute("data-draft") || !by || !by.hasAttribute("formaction")) return;
    event.preventDefault();
    var data = new FormData(form);
    if (by.name) data.append(by.name, by.value);
    fetch(by.formAction, { method: "POST", body: data, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.text();
      })
      .then(function (text) {
        var page = new DOMParser().parseFromString(text, "text/html");
        var fresh = page.querySelector("form[data-draft]");
        if (!fresh) throw new Error("no form");
        form.replaceWith(fresh);
        var focus = null;
        if (by.name === "add") {
          var boxes = fresh.querySelectorAll('input[name="grams"], input[name="amount"]');
          focus = boxes[boxes.length - 1];
        } else if (by.name === "do") {
          focus = fresh.querySelector(".picks button") || fresh.querySelector("[data-draft-search]");
        }
        if (focus) {
          focus.focus({ preventScroll: true });
          if (focus.select) focus.select();
          focus.scrollIntoView({ block: "nearest" });
        }
      })
      .catch(function () {
        form.removeAttribute("data-draft");
        if (form.requestSubmit) form.requestSubmit(by);
        else by.click();
      });
  });

  // New food (#97): USDA results follow what's typed, a moment after typing stops.
  // Open Food Facts allows only about 10 searches a minute, so it waits for Search.
  var dbForm = document.querySelector("form[data-live-usda]");
  var dbResults = document.querySelector("[data-db-results]");
  if (dbForm && dbResults && window.fetch) {
    var dbBox = dbForm.querySelector("input[name=q]");
    var dbTimer = null;
    var dbAsked = 0;
    var dbPending = null;
    var dbCurrent = dbBox.value.trim();
    dbBox.addEventListener("input", function () {
      clearTimeout(dbTimer);
      dbTimer = setTimeout(function () {
        var q = dbBox.value.trim();
        if (q === dbCurrent) return;
        dbCurrent = q;
        var mine = ++dbAsked;
        if (dbPending) dbPending.abort();
        dbPending = null;
        if (q.length < 3) {
          // Too short to search: an answer still on its way, or shown, is for other words
          dbResults.innerHTML = "";
          return;
        }
        dbPending = window.AbortController ? new AbortController() : null;
        var url = dbForm.action + "?" + new URLSearchParams({ q: q }).toString();
        fetch(url + "&part=usda", {
          credentials: "same-origin",
          signal: dbPending ? dbPending.signal : undefined,
        }).then(function (r) {
          if (!r.ok) throw new Error(r.status);
          return r.text();
        }).then(function (html) {
          if (mine !== dbAsked) return;
          dbPending = null;
          dbResults.innerHTML = html;
          try {
            history.replaceState(history.state, "", url);
          } catch (err) {
            // the address just keeps the old search
          }
        }).catch(function () {
          // Offline or aborted: the Search button still works
        });
      }, 500);
    });
  }

  document.querySelectorAll("form").forEach(changed);

  // Imports (#94): a big upload can take a while, so the form turns into a spinner with a
  // running count of seconds until the next page arrives
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var text = form.getAttribute("data-busy");
    if (!text || event.defaultPrevented || form.querySelector(".busy-note")) return;
    var note = document.createElement("div");
    note.className = "busy-note";
    note.setAttribute("role", "status");
    note.innerHTML =
      '<svg class="spinner" viewBox="0 0 50 50" aria-hidden="true">' +
      '<circle cx="25" cy="25" r="20"/></svg>' +
      '<div><div class="busy-text"></div>' +
      '<div class="muted small">Keep this page open; it can take a minute.</div></div>';
    var label = note.querySelector(".busy-text");
    var started = Date.now();
    function tick() {
      var seconds = Math.floor((Date.now() - started) / 1000);
      label.textContent = text + "\u2026" + (seconds ? " " + seconds + " s" : "");
    }
    tick();
    var timer = setInterval(tick, 1000);
    form.classList.add("busy");
    form.appendChild(note);
    // Coming back to this page from the next one shows the form again, not a stale spinner
    window.addEventListener("pageshow", function (shown) {
      if (!shown.persisted) return;
      clearInterval(timer);
      form.classList.remove("busy");
      note.remove();
    }, { once: true });
  });

  // On a screen too narrow for every tab the bar scrolls; keep the current one in sight
  var tabsBar = document.querySelector(".tabs");
  var currentTab = tabsBar && tabsBar.querySelector("[aria-current]");
  if (currentTab && tabsBar.scrollWidth > tabsBar.clientWidth) {
    tabsBar.scrollLeft = currentTab.offsetLeft + currentTab.offsetWidth - tabsBar.clientWidth;
  }
})();
