# Demo programme

A small files-tracker tree you can explore without any account. It holds one programme
(`demo`, kind `vault-docs`), one project (`demo-p1`) with PM `pm1`, a team lead `pm1tl1`,
a senior user `pm1su1` paired with that team lead, and a draft PID.

```bash
cp -R tracker ./ygg-tracker
export YGG_TRACKER=files YGG_FILES_ROOT="$PWD/ygg-tracker" YGG_STATE_ROOT="$PWD/ygg-state"
ygg rehydrate
ygg status
```
