# Glemmen Fleet
Dette git-repositoriet inneholder konfigurasjon for 2ITA sin lille Kubernetes cluster, filene i denne repoen definerer hva som kjører og hvordan det kjører.


Alle får lov til å komme med endringer og eksperimentere med clusteret.
For å legge til eller modifisere noe, lag en fork av dette repositoriet, gjør endringene i din egen fork og lag en pull request når du er ferdig. Vi vil da se over endringene og merge dem inn i hovedrepoet.

## Domene og nettverk
Alle ingress tjenester med et av disse domenene vil automatisk være tilgjengelig på internett via HTTPS:
- *.glem.blatrix.eu
- *.thisisaveryshortdomain.asso.eu.org
- *.shrail.eu.org
- *.glem.men

## Litt om infrastrukturen
Clusteret består av 5 nodes, alle er fysiske maskiner som kjører Ubuntu 24.04 LTS. Clusteret er managed med Rancher og bruker K3S som Kubernetes distribusjon. Alle nodes har alle rollene.
Hardwaren på de 5 serverne er identisk og består av:
- Intel(R) Xeon(R) E-2314 CPU @ 2.80GHz
- 32 GB DDR4 RAM
- 2 TB HDD (ST2000NM012B-2TD130)

Nodene kommuniserer internt med hverandre via Netbird, som er en P2P mesh VPN som gjør at alle nodes kan kommunisere med hverandre selv om de er på forskjellige nettverk.

Nodene har automatisk IP failover via `keepalived` på den interne (ikke Netbird) IP-adressen `192.168.6.99`, failover går i denne rekkefølgen: `glemmen130` -> `glemmen150` -> `glemmen160` -> `glemmen170` -> `glemmen180`.
Failover blir brukt for port forwarding av tjenester som kjører på clusteret, slik at de alltid er tilgjengelig selv om en node går ned.