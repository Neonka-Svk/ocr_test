# Herdyn - sledovanie kodu (GitHub Actions)

Kazdy den bezi automaticky na serveri GitHubu (notebook moze byt vypnuty):

- **15:50 - 21:45** a **21:30 - 03:25** (slovensky cas, v zime)
- cita chat `twitch.tv/herdyn` a OCR-om obraz streamu
- ked najde mozny kod, posle push na mobil cez **ntfy**

## Nastavenie (raz)

1. Do mobilu nainstaluj appku **ntfy** a prihlas sa na odber temy (`+` -> nazov temy).
2. Na GitHube: **Settings -> Secrets and variables -> Actions -> New repository secret**
   - Name: `NTFY_TOPIC`
   - Secret: nazov tvojej temy
3. Test: **Actions -> Herdyn - sledovanie kodu -> Run workflow** (napr. `minutes` = 5).
   Hned na zaciatku pride testovacia notifikacia.

Logy a snimky z kazdeho behu su v Actions pri konkretnom behu (Artifacts), 7 dni.

Pozn.: GitHub automaticke spustenia obcas oneskori o par minut. Ak sa v repozitari 60 dni nic nezmeni, GitHub planovane behy sam vypne - staci potom kliknut Enable.
