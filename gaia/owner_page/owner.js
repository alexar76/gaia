/* GAIA "Your plugs": the owner switches their own plugs and reads their consumption.
 *
 * Every request is an EIP-712 OwnerRequest signed in the owner's wallet (gaia/owner_auth.py):
 * domain {GAIA, 1, chainId 8453}, the GAIA's own public URL, the plug, the action, the time and
 * a random nonce. GAIA recovers the signer and answers only the plug's owner or a delegate.
 * Everything the server returns is inserted as text, never as HTML.
 */
(function () {
  "use strict";

  const I18N = {
    en: { title: "Your plugs", lead: "Only you switch your plugs. Each command is signed by your wallet (EIP-712) — no hub, no payment, no account.",
      connect: "Connect wallet", connected: "Wallet", on_for: "On for", minutes: "min", off: "Off", usage: "Last 24 h",
      no_wallet: "No wallet found. Install a browser wallet (for example MetaMask) and reload.", signing: "Sign in your wallet…",
      switching: "Switching…", verified: "Effect measured", auto_off: "switches itself off at", refused: "Refused",
      none: "This GAIA has no plugs with an owner.", energy: "Energy, Wh", mean: "Mean, W", peak: "Peak, W", onm: "On, min",
      hour: "Hour (UTC)", total: "Total", demo: "The public demo plug has no owner; it is switched through a hub with gaia.actuate@v1.",
      foot: "The signature names this GAIA, the plug, the command and the time; it is good for two minutes, once. Nobody else's wallet can switch a plug you own.",
      sale_on: "consumption statistics for sale", sale_off: "consumption statistics private" },
    ru: { title: "Мои розетки", lead: "Вашими розетками управляете только вы. Каждую команду подписывает ваш кошелёк (EIP-712) — без хаба, без оплаты, без регистрации.",
      connect: "Подключить кошелёк", connected: "Кошелёк", on_for: "Включить на", minutes: "мин", off: "Выключить", usage: "За 24 ч",
      no_wallet: "Кошелёк не найден. Установите браузерный кошелёк (например, MetaMask) и обновите страницу.", signing: "Подпишите в кошельке…",
      switching: "Переключаю…", verified: "Эффект измерен", auto_off: "выключится сама в", refused: "Отказ",
      none: "У этой GAIA нет розеток с владельцем.", energy: "Энергия, Вт·ч", mean: "Средняя, Вт", peak: "Пик, Вт", onm: "Вкл., мин",
      hour: "Час (UTC)", total: "Итого", demo: "У публичной демонстрационной розетки нет владельца; ею управляют через хаб функцией gaia.actuate@v1.",
      foot: "Подпись называет эту GAIA, розетку, команду и время; она действует две минуты и один раз. Чужой кошелёк не может переключить вашу розетку.",
      sale_on: "статистика энергопотребления продаётся", sale_off: "статистика энергопотребления закрыта" },
    es: { title: "Sus enchufes", lead: "Solo usted controla sus enchufes. Cada orden la firma su monedero (EIP-712): sin hub, sin pago, sin cuenta.",
      connect: "Conectar monedero", connected: "Monedero", on_for: "Encender", minutes: "min", off: "Apagar", usage: "Últimas 24 h",
      no_wallet: "No se encontró un monedero. Instale un monedero de navegador (por ejemplo, MetaMask) y recargue.", signing: "Firme en su monedero…",
      switching: "Conmutando…", verified: "Efecto medido", auto_off: "se apaga sola a las", refused: "Rechazado",
      none: "Este GAIA no tiene enchufes con propietario.", energy: "Energía, Wh", mean: "Media, W", peak: "Pico, W", onm: "Encendido, min",
      hour: "Hora (UTC)", total: "Total", demo: "El enchufe de demostración público no tiene propietario; se controla a través de un hub con gaia.actuate@v1.",
      foot: "La firma nombra este GAIA, el enchufe, la orden y la hora; vale dos minutos y una sola vez. El monedero de otra persona no puede conmutar un enchufe suyo.",
      sale_on: "estadísticas de consumo en venta", sale_off: "estadísticas de consumo privadas" },
    fr: { title: "Vos prises", lead: "Vous seul commandez vos prises. Chaque commande est signée par votre portefeuille (EIP-712) : sans hub, sans paiement, sans compte.",
      connect: "Connecter le portefeuille", connected: "Portefeuille", on_for: "Allumer", minutes: "min", off: "Éteindre", usage: "Dernières 24 h",
      no_wallet: "Aucun portefeuille trouvé. Installez un portefeuille de navigateur (par exemple MetaMask) et rechargez.", signing: "Signez dans votre portefeuille…",
      switching: "Commutation…", verified: "Effet mesuré", auto_off: "s'éteint seule à", refused: "Refusé",
      none: "Ce GAIA n'a aucune prise avec un propriétaire.", energy: "Énergie, Wh", mean: "Moyenne, W", peak: "Pointe, W", onm: "Allumée, min",
      hour: "Heure (UTC)", total: "Total", demo: "La prise de démonstration publique n'a pas de propriétaire ; elle se commande via un hub avec gaia.actuate@v1.",
      foot: "La signature nomme ce GAIA, la prise, la commande et l'heure ; elle vaut deux minutes, une seule fois. Le portefeuille d'un autre ne peut pas commuter une prise qui vous appartient.",
      sale_on: "données de consommation en vente", sale_off: "données de consommation privées" },
    zh: { title: "我的插座", lead: "只有您能控制自己的插座。每条指令都由您的钱包签名（EIP-712）——无需枢纽、无需付费、无需账户。",
      connect: "连接钱包", connected: "钱包", on_for: "开启", minutes: "分钟", off: "关闭", usage: "最近 24 小时",
      no_wallet: "未找到钱包。请安装浏览器钱包（例如 MetaMask）后刷新页面。", signing: "请在钱包中签名…",
      switching: "正在切换…", verified: "已测得效果", auto_off: "将自动关闭于", refused: "已拒绝",
      none: "此 GAIA 没有设有所有者的插座。", energy: "电量，Wh", mean: "平均，W", peak: "峰值，W", onm: "开启，分钟",
      hour: "小时（UTC）", total: "合计", demo: "公开演示插座没有所有者；通过枢纽用 gaia.actuate@v1 控制。",
      foot: "签名写明这台 GAIA、插座、指令和时间；有效期两分钟，只能使用一次。别人的钱包无法切换您的插座。",
      sale_on: "电量统计出售中", sale_off: "电量统计不公开" },
  };
  const SUP = Object.keys(I18N);
  let lang = "en";
  try {
    const saved = localStorage.getItem("gaia-owner-lang");
    if (saved && SUP.includes(saved)) lang = saved;
    else { const n = (navigator.language || "en").slice(0, 2).toLowerCase(); if (SUP.includes(n)) lang = n; }
  } catch (_) {}
  const t = (k) => (I18N[lang] && I18N[lang][k]) || I18N.en[k] || k;

  function applyLang() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll(".langsw button").forEach((b) => b.classList.toggle("on", b.dataset.lang === lang));
    if (state.plugs) render();
  }
  document.querySelectorAll(".langsw button").forEach((b) => b.addEventListener("click", () => {
    lang = b.dataset.lang;
    try { localStorage.setItem("gaia-owner-lang", lang); } catch (_) {}
    applyLang();
  }));

  const BASE = { chainId: "0x2105", chainName: "Base", nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
                 rpcUrls: ["https://mainnet.base.org"], blockExplorerUrls: ["https://basescan.org"] };
  const TYPES = {
    EIP712Domain: [{ name: "name", type: "string" }, { name: "version", type: "string" }, { name: "chainId", type: "uint256" }],
    OwnerRequest: [
      { name: "gaia", type: "string" }, { name: "actuator", type: "string" }, { name: "action", type: "string" },
      { name: "durationS", type: "uint256" }, { name: "fromTs", type: "uint256" }, { name: "toTs", type: "uint256" },
      { name: "issuedAt", type: "uint256" }, { name: "nonce", type: "bytes32" },
    ],
  };
  const state = { account: null, gaia: null, plugs: null };
  const $ = (id) => document.getElementById(id);
  const el = (tag, props, ...kids) => {
    const n = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => { if (k === "class") n.className = v; else if (k === "text") n.textContent = v; else n.setAttribute(k, v); });
    kids.forEach((k) => n.append(k));
    return n;
  };

  async function invoke(capability_id, input) {
    const r = await fetch("/ai-market/v2/invoke", { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify({ capability_id, input }) });
    return r.json();
  }

  function nonce() {
    const b = new Uint8Array(32);
    crypto.getRandomValues(b);
    return "0x" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  }

  async function ensureBase() {
    const id = await window.ethereum.request({ method: "eth_chainId" });
    if (id === BASE.chainId) return;
    try {
      await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: BASE.chainId }] });
    } catch (e) {
      if (e && e.code === 4902) await window.ethereum.request({ method: "wallet_addEthereumChain", params: [BASE] });
      else throw e;
    }
  }

  async function signed(actuator, action, extra) {
    await ensureBase();
    const issuedAt = Math.floor(Date.now() / 1000);
    const n = nonce();
    const message = { gaia: state.gaia, actuator, action, durationS: extra.durationS || 0,
                      fromTs: extra.fromTs || 0, toTs: extra.toTs || 0, issuedAt, nonce: n };
    const typed = { types: TYPES, primaryType: "OwnerRequest", domain: { name: "GAIA", version: "1", chainId: 8453 }, message };
    const signature = await window.ethereum.request({ method: "eth_signTypedData_v4",
                                                      params: [state.account, JSON.stringify(typed)] });
    return { signer: state.account, issued_at: issuedAt, nonce: n, signature };
  }

  function say(box, cls, text) { box.className = "out " + cls; box.textContent = text; }

  async function command(plug, cmd, minutes, box, meta) {
    try {
      say(box, "note", t("signing"));
      const durationS = cmd === "on" ? Math.round(minutes * 60) : 0;
      const auth = await signed(plug.actuator_id, cmd, { durationS });
      say(box, "note", t("switching"));
      const body = await invoke("gaia.owner.command@v1", { actuator_id: plug.actuator_id, command: cmd,
                                                           duration_s: durationS || undefined, auth });
      if (body.ok) {
        const o = body.output || {};
        plug.relay_on = cmd === "on";
        meta.textContent = metaText(plug);
        say(box, "ok", t("verified") + ": " + (o.effect && o.effect.observed) + " W" +
            (o.auto_off_at ? " — " + t("auto_off") + " " + o.auto_off_at : ""));
      } else {
        say(box, "err", t("refused") + ": " + (body.error || body.detail || "?"));
      }
    } catch (e) {
      say(box, "err", (e && e.message) || String(e));
    }
  }

  async function usage(plug, box) {
    try {
      say(box, "note", t("signing"));
      const toTs = Math.floor(Date.now() / 1000), fromTs = toTs - 86400;
      const auth = await signed(plug.actuator_id, "consumption", { fromTs, toTs });
      const body = await invoke("gaia.owner.consumption@v1", { actuator_id: plug.actuator_id, from_ts: fromTs,
                                                               to_ts: toTs, resolution: "hour", auth });
      if (!body.ok) return say(box, "err", t("refused") + ": " + (body.error || "?"));
      const o = body.output, peak = Math.max(1, ...o.buckets.map((b) => b.energy_wh));
      box.className = "out"; box.textContent = "";
      const table = el("table", {}, el("tr", {}, el("th", { text: t("hour") }), el("th", { text: t("energy") }),
        el("th", { text: t("mean") }), el("th", { text: t("peak") }), el("th", { text: t("onm") }), el("th", { text: "" })));
      o.buckets.forEach((b) => {
        const bar = el("div", { class: "bar" }); bar.style.width = Math.round(100 * b.energy_wh / peak) + "%";
        table.append(el("tr", {}, el("td", { text: b.start.slice(11, 16) }), el("td", { text: b.energy_wh.toFixed(1) }),
          el("td", { text: b.mean_w.toFixed(0) }), el("td", { text: b.peak_w.toFixed(0) }),
          el("td", { text: b.on_minutes.toFixed(0) }), el("td", { style: "width:30%" }, bar)));
      });
      table.append(el("tr", {}, el("th", { text: t("total") }), el("th", { text: o.totals.energy_wh.toFixed(1) }),
        el("th", { text: "" }), el("th", { text: "" }), el("th", { text: o.totals.on_minutes.toFixed(0) }), el("th", { text: "" })));
      box.append(table);
    } catch (e) {
      say(box, "err", (e && e.message) || String(e));
    }
  }

  function metaText(p) {
    const sale = p.consumption && p.consumption.for_sale ? t("sale_on") : t("sale_off");
    return p.actuator_id + " · " + (p.relay_on ? "ON" : "OFF") + " · " + sale;
  }

  function render() {
    const root = $("plugs");
    root.textContent = "";
    const owned = state.plugs.filter((p) => p.access === "owner");
    if (state.plugs.some((p) => p.access === "public")) root.append(el("p", { class: "out note", text: t("demo") }));
    if (!owned.length) { root.append(el("p", { class: "out", text: t("none") })); return; }
    owned.forEach((p) => {
      const box = el("div", { class: "out" });
      const minutes = el("input", { type: "number", min: "1", max: String(Math.floor(p.max_on_s / 60) || 15),
                                    value: String(Math.min(15, Math.floor(p.max_on_s / 60) || 15)) });
      const on = el("button", { type: "button", text: t("on_for") }), off = el("button", { type: "button", class: "ghost", text: t("off") });
      const use = el("button", { type: "button", class: "ghost", text: t("usage") });
      [on, off, use].forEach((b) => { b.disabled = !state.account; });
      const meta = el("div", { class: "meta", text: metaText(p) });
      on.addEventListener("click", () => command(p, "on", Number(minutes.value || 1), box, meta));
      off.addEventListener("click", () => command(p, "off", 0, box, meta));
      use.addEventListener("click", () => usage(p, box));
      root.append(el("div", { class: "plug" }, el("h2", { text: p.label || p.actuator_id }), meta,
        el("div", { class: "act" }, on, minutes, el("span", { text: t("minutes") }), off, use), box));
    });
  }

  async function load() {
    try {
      const manifest = await (await fetch("/ai-market/v2/manifest")).json();
      state.gaia = String(manifest.base_url || location.origin).replace(/\/+$/, "");
      const list = await invoke("gaia.actuator.list@v1", {});
      state.plugs = (list.output && list.output.actuators) || [];
      render();
    } catch (e) {
      const f = $("fatal"); f.hidden = false; f.textContent = (e && e.message) || String(e);
    }
  }

  $("connect").addEventListener("click", async () => {
    if (!window.ethereum) { const f = $("fatal"); f.hidden = false; f.textContent = t("no_wallet"); return; }
    try {
      const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
      state.account = accounts[0];
      $("who").textContent = t("connected") + ": " + state.account;
      render();
    } catch (e) {
      const f = $("fatal"); f.hidden = false; f.textContent = (e && e.message) || String(e);
    }
  });

  applyLang();
  load();
})();
