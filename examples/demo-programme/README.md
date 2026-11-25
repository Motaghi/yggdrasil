# Demo programme

A small files-tracker tree you can explore without any account. It holds one programme
(`demo`, kind `vault-docs`), one project (`demo-p1`) with PM `pm1`, a team lead `pm1tl1`,
a senior user `pm1su1` paired with that team lead, and a draft PID.

Run it in a fresh, empty folder, with `REPO` set to your checkout of Yggdrasil. `YGG_CONFIG=none` makes `ygg` ignore
any config file you already have, and `unset YGG_PROFILE` drops a profile you may have set. These `export` lines
change your current shell, so use a new terminal or close it afterwards. Don't start a conductor against this state
folder: it would launch a real project manager session on your AI login.

```bash
export REPO=/path/to/your/yggdrasil/checkout
mkdir ygg-demo && cd ygg-demo
export YGG_CONFIG=none
unset YGG_PROFILE
cp -R "$REPO/examples/demo-programme/tracker" ./ygg-tracker
export YGG_TRACKER=files YGG_FILES_ROOT="$PWD/ygg-tracker" YGG_STATE_ROOT="$PWD/ygg-state"
ygg rehydrate
ygg status
```
