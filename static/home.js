(function () {
  var add = document.getElementById("add-player");
  var list = document.getElementById("players");
  if (add && list) {
    add.addEventListener("click", function () {
      var n = list.querySelectorAll('input[name="player"]').length;
      if (n >= 12) {
        add.disabled = true;
        return;
      }
      var label = document.createElement("label");
      label.className = "field";
      var span = document.createElement("span");
      span.textContent = "Player";
      var input = document.createElement("input");
      input.name = "player";
      input.type = "text";
      input.maxLength = 24;
      input.autocomplete = "off";
      input.enterKeyHint = "next";
      label.appendChild(span);
      label.appendChild(input);
      list.appendChild(label);
      input.focus();
      if (n + 1 >= 12) add.disabled = true;
    });
  }
  var copy = document.getElementById("copy-board");
  if (copy && navigator.clipboard) {
    copy.addEventListener("click", function () {
      var url = copy.getAttribute("data-url") || "";
      navigator.clipboard.writeText(url).then(function () {
        var prev = copy.textContent;
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = prev; }, 1200);
      }).catch(function () {});
    });
  }

  var copySum = document.getElementById("copy-summary");
  var summary = document.getElementById("summary");
  if (summary) {
    summary.style.height = "auto";
    summary.style.height = (summary.scrollHeight + 8) + "px";
  }
  if (copySum && summary && navigator.clipboard) {
    copySum.addEventListener("click", function () {
      navigator.clipboard.writeText(summary.value).then(function () {
        var prev = copySum.textContent;
        copySum.textContent = "Copied";
        setTimeout(function () { copySum.textContent = prev; }, 1200);
      }).catch(function () {});
    });
  }

  var big = document.getElementById("big-type");
  if (big) {
    if (window.localStorage && localStorage.getItem("slate-big") === "1") {
      document.body.classList.add("big");
      big.textContent = "Normal type";
    }
    big.addEventListener("click", function () {
      var on = document.body.classList.toggle("big");
      big.textContent = on ? "Normal type" : "Big type";
      if (window.localStorage) localStorage.setItem("slate-big", on ? "1" : "0");
    });
  }

  var boardId = document.body.getAttribute("data-id");
  if (boardId) {
    var refresh = document.querySelector('meta[http-equiv="refresh"]');
    if (refresh) refresh.parentNode.removeChild(refresh);
    var count = document.body.getAttribute("data-rounds") || "0";
    setInterval(function () {
      fetch("/api/board/" + boardId, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (d && String(d.round_count) !== count) location.reload();
        })
        .catch(function () {});
    }, 8000);
  }
})();
