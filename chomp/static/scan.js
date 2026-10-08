/* Chomp New food page (#59): scan a barcode with the phone camera. The camera feed is drawn
   onto a canvas a few times a second and read with the vendored ZXing library
   (zxing.min.js); the first code read twice in a row is typed into the barcode box and
   looked up, exactly as if the person had typed it. Without a camera, the script or
   ZXing, the box still works by hand. */
(function () {
  "use strict";
  var start = document.querySelector("[data-scan-start]");
  var box = document.querySelector("[data-scanner]");
  var form = document.querySelector("[data-barcode-form]");
  if (!start || !box || !form) return;
  var media = navigator.mediaDevices;
  if (!window.ZXing || !media || !media.getUserMedia) {
    start.hidden = true; // e.g. a page not served over HTTPS: the camera is off limits
    return;
  }
  var Z = window.ZXing;
  var video = box.querySelector("video");
  var status = box.querySelector("[data-scan-status]");
  var problem = document.querySelector("[data-scan-error]");
  var input = form.querySelector("input[name=barcode]");
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d", { willReadFrequently: true });
  var hints = new Map();
  hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [
    Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8, Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E,
  ]);
  hints.set(Z.DecodeHintType.TRY_HARDER, true);
  var reader = new Z.MultiFormatReader();
  reader.setHints(hints);
  var stream = null;
  var timer = null;
  var last = null;

  function stop() {
    clearTimeout(timer);
    timer = null;
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    video.srcObject = null;
    box.hidden = true;
    start.hidden = false;
  }

  function fail(text) {
    stop();
    problem.textContent = text;
    problem.hidden = false;
  }

  // ZXing gives UPC-E in its 8-digit compressed form; Chomp's lookups (and the server, which
  // reads 8 digits as EAN-8) need the UPC-A it stands for
  function upcA(upce) {
    var d = upce.substr(1, 6);
    var last = d.charAt(5);
    var body;
    if (last <= "2") body = d.substr(0, 2) + last + "0000" + d.substr(2, 3);
    else if (last === "3") body = d.substr(0, 3) + "00000" + d.substr(3, 2);
    else if (last === "4") body = d.substr(0, 4) + "00000" + d.charAt(4);
    else body = d.substr(0, 5) + "0000" + last;
    return upce.charAt(0) + body + upce.charAt(7);
  }

  function found(code) {
    stop();
    input.value = code;
    status.textContent = "";
    if (navigator.vibrate) navigator.vibrate(60);
    form.submit();
  }

  function read() {
    var w = video.videoWidth;
    var h = video.videoHeight;
    if (!w || !h) return null;
    var scale = Math.min(1, 1280 / Math.max(w, h));
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    try {
      var source = new Z.HTMLCanvasElementLuminanceSource(canvas);
      var result = reader.decodeWithState(new Z.BinaryBitmap(new Z.HybridBinarizer(source)));
      var text = result.getText();
      return result.getBarcodeFormat() === Z.BarcodeFormat.UPC_E ? upcA(text) : text;
    } catch (err) {
      return null; // nothing readable in this frame
    } finally {
      reader.reset();
    }
  }

  function tick() {
    if (!stream) return;
    var code = read();
    if (code && code === last) return found(code);
    last = code;
    timer = setTimeout(tick, 120);
  }

  start.addEventListener("click", function () {
    problem.hidden = true;
    box.hidden = false;
    start.hidden = true;
    last = null;
    status.textContent = "Starting the camera…";
    media.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
    }).then(function (s) {
      stream = s;
      video.setAttribute("playsinline", ""); // iPhone: play in the page, not full screen
      video.muted = true;
      video.srcObject = s;
      return video.play();
    }).then(function () {
      status.textContent = "Hold the barcode inside the frame, steady and level.";
      tick();
    }).catch(function (err) {
      fail(err && err.name === "NotAllowedError"
        ? "Camera access is off for this site. Allow it in your browser settings, or type the number below."
        : "Couldn't start the camera. Type the number under the barcode below instead.");
    });
  });

  box.querySelector("[data-scan-stop]").addEventListener("click", stop);
  window.addEventListener("pagehide", stop);
})();
