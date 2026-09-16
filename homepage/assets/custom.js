// Injects the "Om clusteret" section between the service groups and the
// footer. Loaded by Homepage from /api/config/custom.js on every page load.
(function () {
  function addAboutSection() {
    var footer = document.getElementById("footer");
    if (!footer || document.querySelector(".about-cluster")) return;

    var section = document.createElement("section");
    section.className = "about-cluster";
    section.innerHTML =
      "<h2>Om Glemmen Fleet</h2>" +
      "<p>Dette dashbordet viser sanntidsstatus for Kubernetes-clusteret til 2ITA. " +
      "Clusteret består av seks fysiske servere som kjører Ubuntu 24.04 LTS og K3s, " +
      "og administreres med Rancher. All konfigurasjon ligger i Git og rulles ut " +
      "automatisk med Fleet.</p>" +
      "<dl>" +
      "<div><dt>Noder</dt><dd>glemmen80, glemmen130, glemmen150, glemmen160, " +
      "glemmen170 og glemmen180</dd></div>" +
      "<div><dt>Maskinvare</dt><dd>5 × Intel Xeon E-2314, 32 GB RAM og 2 TB HDD. " +
      "glemmen80: Intel Xeon E5-1650 v3, 192 GB RAM og 1 TB RAID 1.</dd></div>" +
      "<div><dt>Plattform</dt><dd>Ubuntu 24.04 LTS, K3s og Rancher</dd></div>" +
      "<div><dt>Nettverk</dt><dd>Netbird med keepalived-failover på " +
      "192.168.6.99</dd></div>" +
      "<div><dt>Overvåking</dt><dd>Prometheus, Grafana, Longhorn og UniFi Poller</dd></div>" +
      "<div><dt>Kildekode</dt><dd><a href=\"https://github.com/Blalange/glemmen-fleet\" " +
      "target=\"_blank\" rel=\"noreferrer\">github.com/Blalange/glemmen-fleet</a></dd></div>" +
      "</dl>";

    footer.parentNode.insertBefore(section, footer);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", addAboutSection);
  } else {
    addAboutSection();
  }
})();
