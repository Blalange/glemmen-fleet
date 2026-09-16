// Injects the "Om clusteret" section (assets/about.html, served as
// /about.html) between the service groups and the footer. Edit about.html to
// change the content; no JavaScript changes are needed.
(function () {
  function inject() {
    var footer = document.getElementById("footer");
    if (!footer || document.querySelector(".about-cluster")) return;

    fetch("/about.html")
      .then(function (response) {
        return response.ok ? response.text() : "";
      })
      .then(function (html) {
        if (html.trim()) footer.insertAdjacentHTML("beforebegin", html);
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", inject);
  } else {
    inject();
  }
})();
