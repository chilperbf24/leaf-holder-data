# 🐹 Hamster Pinball · 仓鼠弹珠

A pachinko-style game powered by a **real hamster**. Drop sunflower seeds onto a
board of acorn pegs, carrot windmill spinners and one big running wheel — the
wheel spins at the **live RPM of Squeektoshi**, the real hamster behind
[Squeekchain](https://squeekchain.com), streamed from the public Squeekchain API.

一款由**真仓鼠**驱动的弹珠游戏：投下葵花籽，穿过橡子钉阵和胡萝卜风车，落进底部粮仓格得分。
盘面正中的跑轮转速，就是 Squeekchain 的真仓鼠 Squeektoshi 此刻的真实转速（公开 API 实时数据）。

## Play / 试玩

- **Live:** https://fromwo.github.io/hamster-seed-pinball/
- **Guide / 玩法介绍：** https://fromwo.github.io/hamster-seed-pinball/guide.html
- Or open `index.html` in any browser — single file, no build step, no dependencies.

## How it uses Squeekchain / 如何接入 Squeekchain

- 🎡 **Live wheel data** — `GET api.squeekchain.com/v1/status` (polled every 5 s):
  the wheel's spin, fling strength and the center bin's value follow Squeektoshi's
  real RPM and run/sleep state. Sleeping switches the board to night mode (slower
  gravity, +10% bin scores). The daily board is seeded by date + live block height,
  and when an on-chain settlement round completes mid-game, the player gets a
  golden seed.
- 👛 **Your real account, read live** — connect a wallet (Sepolia) and the asset
  panel reads `/v1/accounts/<address>`: your SQK balance, Squeeks, mining power
  and claimable SQK, straight from the chain.
- 📅 **Daily on-chain check-in** — one click sends `approve(self, 0)` to the SQK
  token contract (`0x6872bfa5ccc3e9e440c6c16e5ffdabb8dba4cc76`) on Sepolia — the
  same kind of transaction Genesis counts for The Tester badge — and pays out
  in-game rewards with a streak bonus.

## The game / 玩法

- 🗺️ **50-stage campaign** across 5 boards （木屑原野 / 瓜子山谷 / 月夜粮仓 /
  十字谷仓 / 之字麦田） with rising target scores, faster beetles and extra
  spinners; clear a stage to unlock the next.
- 🌻 **Multi-ball** — up to 3 seeds in flight at once. Acorn pegs +1, same-bin
  combos ×1.5, 20 peg hits light the harvest lamp (next seed ×2), cheek-pouch
  side pockets refund the seed, golden seeds score double.
- ⚠️ **Risk** — two stinky corners (-20) and a patrolling beetle that steals
  your seed (-10) and resets your combo.
- ⬆️ **Progression** — hamster levels 1–6 with visible cosmetics (scarf →
  sunglasses → bell → crown), four upgrade tracks bought with Golden Seeds.

## Economy / 经济系统

Two in-game currencies in a closed loop — nothing is a token, nothing leaves the game:

- **金瓜子 Golden Seeds** — paid **only on a stage's first clear**
  (40 + 3 × stage) plus the daily check-in. Spent on upgrades and stamina.
- **吱吱币 Squeak Coins** — first clears (25 + 2 × stage) plus the
  **Hamster Miner**: a little mining rig whose Power comes only from its level
  (Power = level × 40, max Lv.8) and which produces
  0.02 coins per Power per hour. Output is stored for at most **12 hours** —
  claim it or production stops. Mining needs a connected wallet; guests don't mine.
- Coins flow back out through the supply chest, lucky bell, swaps and the
  miner lamp, so the loop doesn't just inflate: play → earn → upgrade the
  miner → mine faster → spend again.
- **Stamina** (10 max, 1 per run, +1 every 30 min) paces play.

## Wallet & saves / 钱包与存档

Connect an EVM wallet on Sepolia and sign once: saves, leaderboard, check-in
streak and miner are keyed to your address. Guest play works without a wallet
(local save), except mining. The local leaderboard keeps each player's best
score / level / stage on this device.

## Tech / 技术

Single-file HTML + Canvas + hand-rolled physics. No framework, no build, no
backend, no tracking. State in `localStorage`, keyed per wallet address.
Bilingual UI (English by default, 🌐 switches to 中文）.

## Disclaimer / 声明

Community game built for the Squeekchain Genesis Code program. Not affiliated
with or endorsed by the Squeekchain team. No betting, no token sales; in-game
currencies cannot be exchanged for anything. Sepolia testnet only — test
assets have no value.
