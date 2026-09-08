# Glemmen Fleet
Dette git-repositoriet inneholder konfigurasjon for 2ITA sin lille Kubernetes cluster, filene i denne repoen definerer hva som kjører og hvordan det kjører.


Alle får lov til å komme med endringer og eksperimentere med clusteret.
For å legge til eller modifisere noe, lag en fork av dette repositoriet, gjør endringene i din egen fork og lag en pull request når du er ferdig. Vi vil da se over endringene og merge dem inn i hovedrepoet.

## Domene og nettverk
Alle ingress tjenester med et `*.glem.blatrix.eu` domene vil automatisk være tilgjengelig på internett via HTTPS, for tiden så er dette det eneste domenet som er satt opp for clusteret, men vi kan legge til flere hvis det er ønskelig.