# Tourney Tracker (site)

- `index.html` is the whole site: home, tournament search, organiser accounts and the tournament pages. The `window.TT_LIVE = ...` line near the top holds the Firebase web config.
- `detector/` runs every ~10 minutes (`.github/workflows/detect.yml`). It finds games for tournaments that are on, parses the replays, and puts each game in that tournament's **Confirm games** queue. It needs the `FIREBASE_SERVICE_ACCOUNT` repository secret.
- `firestore.rules` is a copy of the rules pasted into Firebase (the live ones are in the Firebase console).

Never commit the service account key file.
