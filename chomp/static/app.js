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
  document.addEventListener("change", function (event) {
    if (event.target.name === "by") changed(event.target.form);
  });

  // Forms on the page, and in a sheet once it opens (#105): sliders, servings, previews,
  // menus and Log food's live search
  function enhance(root) {
    // A slider and its number box move together
    root.querySelectorAll(".amount").forEach(function (box) {
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

    root.querySelectorAll("[data-servings]").forEach(function (box) {
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
    // "Move to" and "Add to" menus: the time box only matters for a new meal
    // A food's photo (#74): choosing a picture sends it, no Save button needed. In a sheet
    // the sheet sends it once it has closed
    root.querySelectorAll("input[type=file][data-autosubmit]").forEach(function (file) {
      file.addEventListener("change", function () {
        if (!file.files.length) return;
        file.closest("label").classList.add("busy");
        if (file.form.requestSubmit) file.form.requestSubmit();
        else file.form.submit();
      });
    });
    // A saved meal's "Today after this" (#60) only shows while the new meal is on today
    root.querySelectorAll("[data-today]").forEach(function (note) {
      var date = note.closest("form").querySelector('input[name="date"]');
      if (!date) return;
      function sync() {
        note.hidden = date.value !== note.dataset.today;
      }
      date.addEventListener("input", sync);
      date.addEventListener("change", sync);
    });
    root.querySelectorAll("select[data-new-meal]").forEach(function (select) {
      var time = select.form.querySelector(".new-meal");
      var sync = function () {
        if (time) time.hidden = select.value !== "new";
      };
      select.addEventListener("change", sync);
      sync();
    });

    root.querySelectorAll("form").forEach(changed);
    root.querySelectorAll("form[data-live-results]").forEach(liveResults);
  }

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

  // Today's entries (#105) swipe right to show Delete (.swipe-action.start) and hold to edit.
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
      if (child.classList.contains("confirm") || child.classList.contains("start")) left = child;
      else right = child;
    });
    var details = body.querySelector(":scope > details");
    var summary = details && details.querySelector("summary");
    var press = summary || body.querySelector(".food-link") || body; // a weigh-in: the whole row
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
      else if (li.dataset.sheetHold) openSheet(li.dataset.sheetHold, press); // a weigh-in
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
        else if (li.dataset.sheetHold) openSheet(li.dataset.sheetHold, press);
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
    var more = lifted.querySelector(".more"); // the menu is open already
    if (more) more.remove();
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
    if (li.dataset.log) {
      item("Log it", function () {
        close();
        openSheet(li.dataset.log, body.querySelector(".food-link"));
      });
    }
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
      if (event.key === "Tab") trapFocus(layer, event);
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
      results.setAttribute("aria-busy", "true");
      fetch(url + (q ? "&" : "?") + "part=1" + (typing && q ? "&typing=1" : ""), {
        credentials: "same-origin",
        signal: pending ? pending.signal : undefined,
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      }).then(function (html) {
        if (mine !== asked) return;
        pending = null;
        results.removeAttribute("aria-busy");
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
        results.removeAttribute("aria-busy");
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
      if (event.key === "Tab") trapFocus(sheet, event);
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
    var row = unit.closest("[data-stepper]") || unit.parentNode; // the stepper it's inside
    var box = row.querySelector('input[name="amount"]');
    if (!box) return;
    var size = parseFloat(unit.dataset.serving);
    var value = parseFloat(box.value.replace(/,/g, ""));
    if (!size || !isFinite(value)) return;
    var next = unit.value === "g" ? value * size : value / size;
    box.value = String(Math.round(next * 100) / 100);
  });


  // Steppers (#105): − and + change the box beside them a step at a time, never under its
  // minimum. In the saved meal editor the step follows the unit picked beside the box
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-stepper] [data-dir]");
    if (!button) return;
    var box = button.closest("[data-stepper]");
    var input = box.querySelector("input");
    var unit = box.querySelector("select[name=unit]");
    var step = parseFloat(unit && unit.value === "g" && box.dataset.stepG ? box.dataset.stepG : box.dataset.step);
    var low = parseFloat(box.dataset.min) || 0;
    var now = parseFloat(input.value.replace(/,/g, ""));
    if (!isFinite(now)) now = 0;
    var dir = parseInt(button.dataset.dir, 10);
    // Off-step values land on the next step in that direction first
    var next = dir > 0 ? Math.floor(now / step + 1e-9) * step + step : Math.ceil(now / step - 1e-9) * step - step;
    next = Math.max(low, Math.round(next * 100) / 100);
    input.value = String(next);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
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

  enhance(document);

  // New food (#105): an old link to its #scan opens the scanner, now a page of its own
  if (location.hash === "#scan" && document.querySelector("[data-chooser]")) {
    location.replace(location.pathname + "?how=scan");
  }
  // Cancel at the top of a full-screen flow goes back where it was opened from, even
  // after steps inside the flow (a search, a lookup) have added pages of their own
  function inFlow(href) {
    try {
      var u = new URL(href);
      return u.origin === location.origin && u.pathname.indexOf("/foods/new") !== -1;
    } catch (err) {
      return false;
    }
  }
  function flowStart(value) {
    try {
      if (value === undefined) return sessionStorage.getItem("flowFrom");
      sessionStorage.setItem("flowFrom", value);
    } catch (err) {
      return null;
    }
  }
  if (document.querySelector("a[data-back]") && !inFlow(document.referrer)) {
    var opener = document.referrer;
    flowStart(opener && new URL(opener).origin === location.origin ? opener : "");
  }
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[data-back]");
    if (!link || event.defaultPrevented) return;
    var from = document.referrer;
    if (!from || new URL(from).origin !== location.origin) return;
    event.preventDefault();
    if (!inFlow(from) && history.length > 1) history.back();
    else location.href = (inFlow(location.href) && flowStart()) || link.href;
  });

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

  // Sheets (#105): a link marked data-sheet (the + in the tab bar, an entry on Today, a
  // meal's …, a weigh-in) opens its page's ?part=1 over the current page instead. Inside a
  // sheet, a data-sheet link opens over the last one (‹ goes back to it) and a
  // data-sheet-swap link replaces it. A tap outside, the ×, Escape, a drag down or the back
  // button closes it; another link inside goes on to its page, and a form posts as usual
  // once the sheet is closed. Without this, every link opens the page itself.
  var sheetNow = null;
  var ICON_BACK = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>';
  var ICON_CLOSE = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
  function part(href) {
    var from = location.pathname + location.search;
    return href + (href.indexOf("?") < 0 ? "?" : "&") + "part=1&from=" + encodeURIComponent(from);
  }
  function getPart(href) {
    return fetch(part(href), { credentials: "same-origin" }).then(function (r) {
      if (!r.ok || r.redirected) throw new Error(r.status);
      return r.text();
    });
  }
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || !window.fetch) return;
    var link = event.target.closest && event.target.closest("a[data-sheet], a[data-sheet-swap]");
    if (!link) return;
    var inSheet = sheetNow && sheetNow.sheet.contains(link);
    if (!inSheet && !link.hasAttribute("data-sheet")) return; // a plain link on its own page
    event.preventDefault();
    if (inSheet) sheetNow.load(link.href, link.hasAttribute("data-sheet"));
    else openSheet(link.href, link);
  });
  function openSheet(href, opener) {
    if (sheetNow) return;
    getPart(href).then(function (html) {
      var layer = document.createElement("div");
      layer.className = "sheet-layer";
      var sheet = document.createElement("div");
      sheet.className = "sheet";
      sheet.setAttribute("role", "dialog");
      sheet.setAttribute("aria-modal", "true");
      sheet.tabIndex = -1;
      sheet.innerHTML =
        '<div class="grab"></div><div class="sheet-bar">' +
        '<button type="button" class="iconbtn sheet-back" aria-label="Back" hidden>' + ICON_BACK +
        '</button><button type="button" class="iconbtn sheet-close" aria-label="Close">' +
        ICON_CLOSE + "</button></div><div class=\"sheet-content\"></div>";
      var content = sheet.querySelector(".sheet-content");
      var backButton = sheet.querySelector(".sheet-back");
      var stack = [];
      layer.appendChild(sheet);

      function show(html, focus) {
        content.innerHTML = html;
        sheet.classList.toggle("full", !!content.querySelector("[data-sheet-full]"));
        var title = content.querySelector("h2[id]");
        if (title) sheet.setAttribute("aria-labelledby", title.id);
        backButton.hidden = stack.length < 2;
        sheet.scrollTop = 0;
        enhance(content);
        if (focus) sheet.focus({ preventScroll: true });
      }
      function load(to, push) {
        return getPart(to).then(function (fresh) {
          if (push) stack.push(fresh);
          else stack[stack.length - 1] = fresh;
          show(fresh, push);
        }).catch(function () {
          location.href = to;
        });
      }
      backButton.addEventListener("click", function () {
        stack.pop();
        show(stack[stack.length - 1], true);
      });
      sheet.querySelector(".sheet-close").addEventListener("click", dismiss);

      function close() {
        layer.classList.remove("on");
        document.removeEventListener("keydown", onKey);
        window.removeEventListener("popstate", close);
        sheetNow = null;
        setTimeout(function () {
          layer.remove();
        }, 320);
        if (opener && opener.isConnected) opener.focus({ preventScroll: true });
      }
      function dismiss() {
        if (history.state && history.state.sheet) history.back();
        else close();
      }
      function onKey(event) {
        if (event.key === "Escape") dismiss();
        if (event.key === "Tab") trapFocus(sheet, event);
      }
      layer.addEventListener("click", function (event) {
        if (event.target === layer) dismiss();
      });
      // A link that leaves takes the sheet's history entry's place, so Back returns to the
      // page the sheet opened over
      sheet.addEventListener("click", function (event) {
        if (event.defaultPrevented) return;
        var to = event.target.closest("a[href]");
        if (!to || to.target || to.hasAttribute("data-sheet") || to.hasAttribute("data-sheet-swap") ||
            !history.state || !history.state.sheet) return;
        event.preventDefault();
        location.replace(to.href);
      });
      // A form: a search loads into the sheet; a post goes once the sheet's history entry
      // is gone, so Back from the page it lands on doesn't reopen this one
      sheet.addEventListener("submit", function (event) {
        var form = event.target;
        if (event.defaultPrevented) return;
        event.preventDefault();
        if (form.method.toLowerCase() === "get") {
          load(form.action + "?" + new URLSearchParams(new FormData(form)).toString(), false);
          return;
        }
        var by = event.submitter;
        if (by && by.name) {
          var keep = document.createElement("input");
          keep.type = "hidden";
          keep.name = by.name;
          keep.value = by.value;
          form.appendChild(keep);
        }
        if (by) by.disabled = true;
        function send() {
          document.body.appendChild(form); // outlives the sheet
          form.hidden = true;
          form.submit();
        }
        if (history.state && history.state.sheet) {
          window.addEventListener("popstate", function once() {
            window.removeEventListener("popstate", once);
            send();
          });
          history.back();
        } else {
          send();
        }
      });
      // Drag the sheet down to dismiss it
      var startY = null;
      sheet.addEventListener("touchstart", function (event) {
        startY = sheet.scrollTop <= 0 ? event.touches[0].clientY : null;
      }, { passive: true });
      sheet.addEventListener("touchmove", function (event) {
        if (startY === null) return;
        var dy = event.touches[0].clientY - startY;
        if (dy > 0) sheet.style.transform = "translateY(" + dy + "px)";
      }, { passive: true });
      sheet.addEventListener("touchend", function (event) {
        if (startY === null) return;
        var dy = event.changedTouches[0].clientY - startY;
        sheet.style.transform = "";
        startY = null;
        if (dy > 80) dismiss();
      });

      sheetNow = { sheet: sheet, load: load };
      stack.push(html);
      document.body.appendChild(layer);
      show(html, true);
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          layer.classList.add("on");
        });
      });
      try {
        history.pushState({ sheet: true }, "");
        window.addEventListener("popstate", close);
      } catch (err) {
        // the back button just leaves the page then
      }
      document.addEventListener("keydown", onKey);
    }).catch(function () {
      location.href = href;
    });
  }

  // Log food (#105): results follow what's typed, a moment after typing stops, without
  // leaving the sheet or reloading the page. Typing searches your foods and saved meals
  // only (live=1); Enter also asks USDA when few of yours match. An older answer never
  // replaces a newer one.
  function liveResults(form) {
    var box = form.querySelector("input[name=q]");
    if (!box || !window.fetch) return;
    var timer = null;
    var asked = 0;
    function run(live) {
      var params = new URLSearchParams(new FormData(form));
      var url = form.action + "?" + params.toString();
      if (live) params.set("live", "1");
      params.set("part", "1");
      var mine = ++asked;
      var shown = form.parentNode.querySelector("[data-results]");
      if (shown) shown.setAttribute("aria-busy", "true");
      fetch(form.action + "?" + params.toString(), { credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error(r.status);
          return r.text();
        })
        .then(function (html) {
          if (mine !== asked) return;
          var page = new DOMParser().parseFromString(html, "text/html");
          var root = form.parentNode;
          var fresh = page.querySelector("[data-results]");
          var old = root.querySelector("[data-results]");
          if (!fresh || !old) return;
          old.replaceWith(fresh);
          var pills = root.querySelector(":scope > .pills");
          var freshPills = page.querySelector(".logfood > .pills");
          if (pills) pills.remove();
          if (freshPills) form.after(freshPills);
          enhance(fresh);
          if (!form.closest(".sheet")) {
            try {
              history.replaceState(history.state, "", url);
            } catch (err) {
              // the address just keeps the old search
            }
          }
        })
        .catch(function () {
          var still = form.parentNode.querySelector("[data-results]");
          if (still) still.removeAttribute("aria-busy");
          // Offline: Enter still submits the form
        });
    }
    box.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        run(true);
      }, 300);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      clearTimeout(timer);
      run(false);
    });
  }

  // Coming back to Today or Progress after five minutes away (the phone locked, another
  // app) reloads it, so it shows what arrived meanwhile (#105). Not while a sheet or menu
  // is open or something is being typed. Notices of edits made before don't come back.
  var AWAY_MS = 5 * 60 * 1000;
  var loadedAt = Date.now();
  var awayAt = null;
  function busy() {
    var active = document.activeElement;
    return !!document.querySelector(".sheet-layer, .cal-layer, .hold-layer") ||
      (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName));
  }
  function refresh() {
    var url = new URL(location.href);
    ["done", "undone", "problem", "weighed"].forEach(function (k) {
      url.searchParams.delete(k);
    });
    location.replace(url.pathname + url.search + url.hash);
  }
  if (document.body.hasAttribute("data-refresh")) {
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        awayAt = Date.now();
        return;
      }
      if (awayAt !== null && Date.now() - awayAt >= AWAY_MS && !busy()) refresh();
      awayAt = null;
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && Date.now() - loadedAt >= AWAY_MS && !busy()) refresh();
    });
  }

  // A screen's large title (#105) shows small in a bar at the top once it scrolls away
  var miniWatch = null;
  function watchTitle() {
    var bigTitle = document.querySelector("h1[data-title]");
    var mini = document.querySelector(".minihead");
    if (miniWatch) miniWatch.disconnect();
    if (mini) mini.classList.remove("on");
    if (!bigTitle || !window.IntersectionObserver) return;
    if (!mini) {
      mini = document.createElement("div");
      mini.className = "minihead";
      mini.setAttribute("aria-hidden", "true");
      document.body.appendChild(mini);
    }
    mini.textContent = bigTitle.textContent;
    miniWatch = new IntersectionObserver(function (seen) {
      mini.classList.toggle("on", !seen[0].isIntersecting);
    });
    miniWatch.observe(bigTitle);
  }
  watchTitle();

  // Appearance on You (#105): System, Dark or Light, kept on this device (theme.js applies it)
  var picks = document.querySelectorAll("[data-theme-pick]");
  function showTheme() {
    var now = document.documentElement.getAttribute("data-theme") || "system";
    picks.forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-pick") === now));
    });
  }
  picks.forEach(function (b) {
    b.addEventListener("click", function () {
      var theme = b.getAttribute("data-theme-pick");
      if (theme === "system") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", theme);
      try {
        if (theme === "system") localStorage.removeItem("chomp-theme");
        else localStorage.setItem("chomp-theme", theme);
      } catch (err) {
        // private browsing: it applies to this page only
      }
      showTheme();
    });
  });
  if (picks.length) showTheme();
  // Charts (#105): press and drag across a chart, or use the arrow keys once it has focus,
  // to read each point in the line above it; a crosshair marks it and the other bars dim.
  // Letting go (or a tap) pins the point, with a link to its day where it has one; a tap
  // anywhere else lets it go. Compare shows the previous period as outlined bars. The
  // points come from the figure's data-points, written by chomp/charts.py.
  function initCharts(root) {
    root.querySelectorAll("[data-chart]").forEach(function (fig) {
      var svg = fig.querySelector("svg.chart");
      var line = fig.querySelector(".inspect");
      var points;
      try {
        points = JSON.parse(fig.getAttribute("data-points"));
      } catch (err) {
        return;
      }
      if (!svg || !line || !points.length) return;
      var summary = line.innerHTML;
      var cross = svg.querySelector(".cross");
      var width = svg.viewBox.baseVal.width;
      var marks = {};
      svg.querySelectorAll("[data-i]").forEach(function (m) {
        marks[m.getAttribute("data-i")] = m;
      });
      var current = -1;
      var pinned = false;
      var dragging = false;

      function add(tag, value) {
        var el = document.createElement(tag);
        el.textContent = value;
        line.appendChild(el);
        return el;
      }
      function show(i) {
        if (marks[current]) marks[current].classList.remove("on");
        current = i;
        if (i < 0) {
          svg.classList.remove("scrub");
          line.innerHTML = summary;
          return;
        }
        var p = points[i];
        svg.classList.add("scrub");
        if (marks[i]) marks[i].classList.add("on");
        cross.setAttribute("x1", p.x);
        cross.setAttribute("x2", p.x);
        line.textContent = "";
        add("span", p.title);
        add("b", p.value);
        if (p.note) add("span", p.note);
        if (p.prev && fig.classList.contains("compare")) add("span", p.prev);
        if (p.href && pinned) add("a", "Open day \u203a").href = p.href;
      }
      function nearest(event) {
        var box = svg.getBoundingClientRect();
        var x = ((event.clientX - box.left) / box.width) * width;
        var best = 0;
        points.forEach(function (p, i) {
          if (Math.abs(p.x - x) < Math.abs(points[best].x - x)) best = i;
        });
        return best;
      }
      function clear() {
        pinned = false;
        show(-1);
      }
      svg.addEventListener("pointerdown", function (event) {
        dragging = true;
        pinned = false;
        show(nearest(event));
      });
      svg.addEventListener("pointermove", function (event) {
        if (dragging || (event.pointerType === "mouse" && !pinned)) show(nearest(event));
      });
      svg.addEventListener("pointerup", function () {
        if (!dragging) return;
        dragging = false;
        pinned = true;
        show(current);
      });
      // A vertical swipe scrolls the page instead (touch-action: pan-y), which cancels this
      svg.addEventListener("pointercancel", function () {
        dragging = false;
        if (!pinned) show(-1);
      });
      svg.addEventListener("pointerleave", function (event) {
        if (event.pointerType === "mouse" && !pinned && !dragging) show(-1);
      });
      document.addEventListener("pointerdown", function (event) {
        if (pinned && !svg.contains(event.target) && !line.contains(event.target)) clear();
      });
      svg.addEventListener("keydown", function (event) {
        var step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
        if (event.key === "Escape" && current >= 0) {
          clear();
          return;
        }
        if (!step) return;
        event.preventDefault();
        var i = current < 0 ? (step < 0 ? points.length - 1 : 0) : current + step;
        pinned = true;
        show(Math.max(0, Math.min(points.length - 1, i)));
      });
      var compare = fig.querySelector("[data-compare]");
      if (compare) {
        compare.addEventListener("click", function () {
          var on = !fig.classList.contains("compare");
          fig.classList.toggle("compare", on);
          compare.setAttribute("aria-pressed", String(on));
          if (current >= 0) show(current);
        });
      }
    });
  }
  initCharts(document);

  var reduceMotion = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);

  // Tab stays inside an open sheet or menu (#105)
  function trapFocus(box, event) {
    var all = Array.prototype.filter.call(
      box.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]):not([type=hidden])," +
        " select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"),
      function (el) { return !el.hidden && el.offsetParent !== null; });
    if (!all.length) return;
    var first = all[0];
    var last = all[all.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === box)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // The sidebar's areas under Progress and Foods (#105): the one open is marked
  function markArea() {
    document.querySelectorAll(".tabs .subnav").forEach(function (nav) {
      var best = null;
      nav.querySelectorAll("a").forEach(function (a) {
        a.removeAttribute("aria-current");
        var p = a.pathname;
        if ((location.pathname === p || location.pathname.indexOf(p + "/") === 0) &&
            (!best || p.length > best.pathname.length)) best = a;
      });
      if (best && nav.previousElementSibling && nav.previousElementSibling.hasAttribute("aria-current")) {
        best.setAttribute("aria-current", "page");
      }
    });
  }
  markArea();

  // Big numbers count up the first time a page shows in a visit (#105)
  function countUp(root) {
    if (reduceMotion || !window.requestAnimationFrame) return;
    var seen = [];
    try {
      seen = JSON.parse(sessionStorage.getItem("chomp-counted") || "[]");
      if (seen.indexOf(location.pathname) >= 0) return;
      seen.push(location.pathname);
      sessionStorage.setItem("chomp-counted", JSON.stringify(seen));
    } catch (err) {
      return; // no storage: no way to do it only once, so not at all
    }
    root.querySelectorAll(".ring-num b, .hero-metric .display").forEach(function (el) {
      var node = el.firstChild;
      if (!node || node.nodeType !== 3) return;
      var m = /^(\D*)(\d[\d,]*(?:\.\d+)?)([\s\S]*)$/.exec(node.nodeValue);
      if (!m) return;
      var target = parseFloat(m[2].replace(/,/g, ""));
      var places = (m[2].split(".")[1] || "").length;
      if (!(target > 0)) return;
      var start = null;
      function frame(t) {
        if (start === null) start = t;
        var k = Math.min(1, (t - start) / 700);
        var v = target * (1 - Math.pow(1 - k, 3));
        node.nodeValue = m[1] + v.toLocaleString("en-US", {
          minimumFractionDigits: places, maximumFractionDigits: places,
          useGrouping: m[2].indexOf(",") >= 0,
        }) + m[3];
        if (k < 1) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    });
  }
  countUp(document);

  // Progress (#105): a range, metric or area changes in place, the charts morphing to the
  // new one, instead of loading a whole page. Back and Forward step through them the same
  // way. Without the script these are plain links.
  var PROGRESS = /\/progress(\/|$)/;
  var shownUrl = location.pathname + location.search;
  function initPage(root) {
    enhance(root);
    root.querySelectorAll("details[data-fold]").forEach(foldable);
    root.querySelectorAll("[data-swipe]").forEach(swipeable);
    root.querySelectorAll("select[data-autosubmit]").forEach(autosubmit);
    initCharts(root);
    watchTitle();
    markArea();
  }
  function swapTo(url, push) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok || r.redirected) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      var page = new DOMParser().parseFromString(html, "text/html");
      var fresh = page.querySelector("main");
      var main = document.querySelector("main");
      if (!fresh || !main) throw new Error("no page");
      function apply() {
        main.innerHTML = fresh.innerHTML;
        document.title = page.title;
        var hue = page.body.getAttribute("data-hue");
        if (hue) document.body.setAttribute("data-hue", hue);
        else document.body.removeAttribute("data-hue");
        initPage(main);
      }
      if (push) history.pushState({ swap: true }, "", url);
      shownUrl = location.pathname + location.search;
      if (document.startViewTransition && !reduceMotion) document.startViewTransition(apply);
      else apply();
    });
  }
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || !window.fetch || !window.DOMParser) return;
    var link = event.target.closest && event.target.closest(
      "main nav.segment a, main nav.pills a, .tabs .subnav a");
    if (!link || link.origin !== location.origin || !PROGRESS.test(location.pathname) ||
        !PROGRESS.test(link.pathname)) return;
    event.preventDefault();
    if (link.getAttribute("aria-current")) return;
    swapTo(link.href, true).catch(function () {
      location.href = link.href;
    });
  });
  window.addEventListener("popstate", function () {
    var now = location.pathname + location.search;
    if (now === shownUrl || sheetNow || !PROGRESS.test(location.pathname)) return;
    swapTo(location.href, false).catch(function () {
      location.reload();
    });
  });

  // Keyboard shortcuts (#105), listed on the sheet ? opens. They wait while you type in a
  // box, hold a modifier, or have a sheet or menu open.
  var tabs = document.querySelector(".tabs[data-shortcuts]");
  var gAt = 0;
  document.addEventListener("keydown", function (event) {
    if (!tabs || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    var t = event.target;
    if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
    if (document.querySelector(".sheet-layer, .cal-layer, .hold-layer")) return;
    var k = event.key;
    var log = tabs.querySelector(".log");
    if (gAt && Date.now() - gAt < 1500) {
      gAt = 0;
      var to = tabs.querySelector('a[aria-keyshortcuts="g ' + k + '"]');
      if (to) {
        event.preventDefault();
        location.href = to.href;
      }
      return;
    }
    gAt = 0;
    if (k === "g") {
      gAt = Date.now();
    } else if (k === "/") {
      event.preventDefault();
      var box = document.querySelector("main input[name=q], main input[type=search]");
      if (box) {
        box.focus();
        box.select();
      } else if (log) {
        openSheet(log.href.replace(/\/actions(\?|$)/, "/add$1"), log);
      }
    } else if (k === "n" && log) {
      event.preventDefault();
      log.click();
    } else if (k === "?") {
      event.preventDefault();
      openSheet(tabs.getAttribute("data-shortcuts"), document.activeElement);
    } else if (k === "ArrowLeft" || k === "ArrowRight") {
      var step = document.querySelector(
        'main .step[aria-label="' + (k === "ArrowLeft" ? "Previous" : "Next") + ' day"]');
      if (step) {
        event.preventDefault();
        location.href = step.href;
      }
    }
  });

  // Tapping the tab you're on goes back to the top of it (#105)
  if (tabs) {
    tabs.addEventListener("click", function (event) {
      var link = event.target.closest("a[aria-current]");
      if (!link || link.parentNode !== tabs || scrollY < 40) return;
      if (link.pathname !== location.pathname || location.search) return;
      event.preventDefault();
      scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  // The phone's tab bar steps aside while the keyboard is up (#105)
  if (window.visualViewport) {
    var vv = window.visualViewport;
    var typing = function () {
      var a = document.activeElement;
      var field = a && (/^(TEXTAREA|SELECT)$/.test(a.tagName) ||
        (a.tagName === "INPUT" && !/^(range|checkbox|radio|button|submit|file)$/.test(a.type)));
      document.body.classList.toggle("kb", !!field && vv.height < innerHeight * 0.8);
    };
    vv.addEventListener("resize", typing);
    document.addEventListener("focusout", function () {
      setTimeout(typing, 50);
    });
  }

  // Range and metric controls keep your place when they load a page: the next page opens
  // where this one was
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented) return;
    var link = event.target.closest && event.target.closest("nav.segment a");
    if (!link) return;
    try {
      sessionStorage.setItem("chomp-keep", JSON.stringify({ path: link.pathname, y: scrollY }));
    } catch (err) {
      // storage off: the page opens at the top
    }
  });
  try {
    var keep = JSON.parse(sessionStorage.getItem("chomp-keep") || "null");
    sessionStorage.removeItem("chomp-keep");
    if (keep && keep.path === location.pathname) scrollTo(0, keep.y);
  } catch (err) {
    // nothing kept
  }
})();
