/* global fetch, location, history, Blob, URL, confirm, alert */
"use strict";

let task = null; // 当前任务状态（含所见修订号）
let demoInfo = null; // 演示环境信息（peer 地址等），非演示环境为 null

const $ = (id) => document.getElementById(id);

// 演示时可以把请求发往另一个共库进程；日常环境全部走本进程。
function baseUrl() {
  const sel = $("peer-switch");
  return sel && sel.value === "peer" ? demoInfo.peer : "";
}

async function callApi(url, options = {}) {
  const resp = await fetch(url, options);
  const ct = resp.headers.get("content-type") || "";
  const data = ct.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const err = new Error(typeof data === "object" ? data.detail : data);
    err.status = resp.status;
    throw err;
  }
  return data;
}

async function api(path, options = {}) {
  return callApi(baseUrl() + path, options);
}

function toast(msg, ms = 4200) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), ms);
}

function fmtUtc(iso) {
  return iso.replace("T", " ").replace("+00:00", "Z");
}

// ---------- 上传 ----------

async function uploadFile(file, tz) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("timezone", tz);
  try {
    task = await api("/api/tasks", { method: "POST", body: fd });
    history.replaceState(null, "", "#" + task.id);
    render();
  } catch (e) {
    toast("上传失败：" + e.message);
  }
}

$("upload-btn").addEventListener("click", () => {
  const file = $("file").files[0];
  const tz = $("tz").value.trim();
  if (!file) return toast("请选择 CSV 文件");
  if (!tz) return toast("请填写 IANA 时区");
  uploadFile(file, tz);
});

document.querySelectorAll(".sample").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const resp = await fetch("/samples/" + btn.dataset.sample);
    const blob = await resp.blob();
    const file = new File([blob], btn.dataset.sample, { type: "text/csv" });
    $("tz").value = btn.dataset.tz;
    uploadFile(file, btn.dataset.tz);
  });
});

// ---------- 渲染 ----------

const STATUS_TEXT = {
  error: "本地时刻不存在（跳时空洞）",
  ambiguous: "回拨重叠：待人工选择",
  resolved: "已唯一确定",
};

function render() {
  if (!task) return;
  $("task-card").classList.remove("hidden");
  $("task-id").textContent = task.id;
  $("revision").textContent = "#" + task.revision;
  $("task-tz").textContent = task.timezone;

  const banner = $("banner");
  const msgs = {
    error: `有 ${task.error_count} 行的本地时刻在所选时区不存在（跳时空洞），禁止导出。请修正数据或更换时区。`,
    unsolvable: "无解：当前时区下不存在满足整份记录 UTC 严格递增的完整解释，禁止导出。",
    ambiguous: `仍有 ${task.ambiguous_count} 行存在回拨歧义，请人工选择 UTC 候选；全部唯一确定后方可导出。`,
    resolved: "全部行已唯一确定，可以导出。",
  };
  banner.className = "banner " + task.status;
  banner.textContent = msgs[task.status];

  const tbody = $("rows-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const row of task.rows) {
    const tr = document.createElement("tr");
    tr.className = "row-" + row.status;
    const utcCell = document.createElement("td");

    if (row.status === "error") {
      utcCell.textContent = "— 该本地时刻不存在 —";
    } else if (row.viable.length === 1) {
      utcCell.innerHTML =
        `<span class="utc-ok">${fmtUtc(row.viable[0].utc)}</span> ` +
        `<span class="hint">${row.viable[0].label}${row.pinned ? "（人工选定）" : ""}</span>`;
      if (row.pinned) utcCell.appendChild(clearBtn(row));
    } else {
      for (const cand of row.viable) {
        const label = document.createElement("label");
        label.className = "cand";
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "row_" + row.id;
        radio.checked = row.pinned === cand.utc;
        radio.addEventListener("change", () => select(row.id, cand.utc));
        label.appendChild(radio);
        label.appendChild(
          document.createTextNode(` ${fmtUtc(cand.utc)}  ${cand.label}`)
        );
        utcCell.appendChild(label);
      }
      if (row.pinned) utcCell.appendChild(clearBtn(row));
    }

    tr.innerHTML =
      `<td>${row.row_index + 1}</td><td>${row.seq}</td>` +
      `<td>${row.local_time}</td><td>${row.pm25}</td>` +
      `<td class="st-${row.status}">${STATUS_TEXT[row.status]}</td>`;
    tr.appendChild(utcCell);
    tbody.appendChild(tr);
  }

  const canExport = task.status === "resolved";
  $("export-btn").disabled = !canExport;
  $("export-hint").textContent = canExport
    ? "导出将携带当前修订号，服务端会拒绝旧修订导出。"
    : "无解或仍有歧义时禁止导出。";
}

function clearBtn(row) {
  const b = document.createElement("button");
  b.className = "clear";
  b.textContent = "清除选择";
  b.addEventListener("click", () => clearSelection(row.id));
  return b;
}

// ---------- 联动操作（均携带所见修订） ----------

async function mutate(path, body) {
  try {
    task = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    render();
  } catch (e) {
    if (e.status === 409) {
      toast(e.message + "，已刷新最新状态");
      task = await api("/api/tasks/" + task.id);
      render();
    } else {
      toast("操作失败：" + e.message);
    }
  }
}

function select(rowId, utc) {
  mutate(`/api/tasks/${task.id}/selections`, {
    row_id: rowId,
    utc,
    revision: task.revision,
  });
}

function clearSelection(rowId) {
  mutate(`/api/tasks/${task.id}/clear-selection`, {
    row_id: rowId,
    revision: task.revision,
  });
}

$("tz-change-btn").addEventListener("click", () => {
  const tz = prompt("输入新的 IANA 时区（将清空全部人工选择）：", task.timezone);
  if (!tz || tz === task.timezone) return;
  mutate(`/api/tasks/${task.id}/timezone`, { timezone: tz, revision: task.revision });
});

$("export-btn").addEventListener("click", async () => {
  try {
    const resp = await fetch(
      `${baseUrl()}/api/tasks/${task.id}/export?revision=${task.revision}`
    );
    if (!resp.ok) {
      const data = await resp.json();
      toast("导出被拒绝：" + data.detail + "，已刷新最新状态");
      task = await api("/api/tasks/" + task.id);
      render();
      return;
    }
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `task_${task.id}_utc.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    toast("导出失败：" + e.message);
  }
});

$("share-btn").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    toast("任务链接已复制，可分享给协作者（共享任务按修订号并发控制）");
  } catch {
    toast("复制失败，请手动复制地址栏链接");
  }
});

// ---------- 初始化 ----------

(async function init() {
  try {
    const zones = await api("/api/timezones");
    $("tz-list").innerHTML = zones.map((z) => `<option value="${z}">`).join("");
  } catch (e) {
    toast("时区列表加载失败：" + e.message);
  }
  if (location.hash.length > 1) {
    try {
      task = await api("/api/tasks/" + location.hash.slice(1));
      render();
    } catch {
      toast("链接中的任务不存在或已过期");
    }
  }
  initDemo();
})();

// ---------- 演示面板（仅 APP_DEMO_HOOKS=1 时出现） ----------

const demoLog = (msg) => {
  const el = $("demo-log");
  const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.textContent = (el.textContent ? el.textContent + "\n" : "") + line;
  el.scrollTop = el.scrollHeight;
};

function showDemo(status) {
  const el = $("demo-state");
  el.textContent = status || "空闲";
}

async function initDemo() {
  try {
    demoInfo = await callApi("/api/demo/info");
  } catch {
    return; // 非演示环境
  }
  $("demo-card").classList.remove("hidden");
  $("peer-url").textContent = demoInfo.peer || "（未配置 APP_PEER_URL）";
  const canPeer = Boolean(demoInfo.peer);
  if (canPeer) {
    $("peer-switch-wrap").classList.remove("hidden");
  }
}

async function setHook(mode, base = "") {
  const info = await callApi(base + "/api/demo/hooks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  showDemo(mode ? `已设置 ${mode}（${base || "本进程"}）` : "空闲");
  return info;
}

// 等被演示钩子退出的进程被重启（compose restart 策略）。
async function waitPeer(base, seconds = 25) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    try {
      await callApi(base + "/api/timezones");
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 250));
    }
  }
  return false;
}

function demoUpload() {
  const fd = new FormData();
  const blob = new Blob([SAMPLE_CSV], { type: "text/csv" });
  fd.append("file", new File([blob], "demo.csv", { type: "text/csv" }));
  fd.append("timezone", "America/New_York");
  return callApi("/api/tasks", { method: "POST", body: fd });
}

const SAMPLE_CSV =
  "采集序号,本地时间,PM2.5\n" +
  "1,2024-11-03 00:59:00,12.4\n" +
  "2,2024-11-03 01:00:00,13.1\n" +
  "4,2024-11-03 01:59:59,14.0\n" +
  "3,2024-11-03 01:30:00,13.7\n" +
  "5,2024-11-03 02:00:00,15.2\n" +
  "6,2024-11-03 02:30:00,16.8\n";

const demoRow = (st, seq) => st.rows.find((r) => r.seq === seq);
const demoPins = (st) =>
  st.rows.filter((r) => r.pinned).map((r) => `${r.seq}:${r.pinned}`).join(", ") || "无";

async function demoRace(peer) {
  demoLog("① 并发选择：A 与 B 同时基于同一修订提交不同选择");
  const st = await demoUpload();
  const tid = st.id;
  await setHook("race");
  await setHook("race", peer); // 两个进程都在读完快照后等一拍，竞态可控
  demoLog(`任务 ${tid}，初始修订 #${st.revision}，行 2/3 均为回拨歧义`);
  const body = (rid, utc) => ({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ row_id: rid, utc, revision: st.revision }),
  });
  const [a, b] = await Promise.allSettled([
    callApi(`/api/tasks/${tid}/selections`, body(demoRow(st, 2).id, "2024-11-03T05:00:00+00:00")),
    callApi(`${peer}/api/tasks/${tid}/selections`, body(demoRow(st, 3).id, "2024-11-03T06:30:00+00:00")),
  ]);
  await setHook("");
  await setHook("", peer).catch(() => {});
  const describe = (r) =>
    r.status === "fulfilled"
      ? `200 成功，返回修订 #${r.value.revision}`
      : `${r.reason.status} ${r.reason.message}`;
  demoLog(`进程 A: ${describe(a)}`);
  demoLog(`进程 B: ${describe(b)}`);
  const latest = await callApi(`/api/tasks/${tid}`);
  demoLog(`库内最终：修订 #${latest.revision}，人工选择 ${demoPins(latest)}`);
  toast("并发选择演示完成：同一修订只有一份成功");
}

async function demoCrash(peer, when) {
  demoLog(when === "before-commit"
    ? "② 保存中退出：B 在 COMMIT 前退出，旧状态必须完整保留"
    : "③ 保存中退出：B 在 COMMIT 后退出，新版必须已持久化");
  const st = await demoUpload();
  const tid = st.id;
  const rev = st.revision;
  await setHook(when, peer);
  demoLog(`任务 ${tid}，修订 #${rev}，向进程 B 提交 seq2=05:00Z …`);
  let cut = false;
  try {
    await callApi(`${peer}/api/tasks/${tid}/selections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        row_id: demoRow(st, 2).id, utc: "2024-11-03T05:00:00+00:00", revision: rev,
      }),
    });
  } catch (e) {
    cut = true;
    demoLog(`进程 B 连接中断（${e.message || "无响应"}），属预期`);
  }
  if (!cut) demoLog("进程 B 连接意外返回，请检查是否启用了演示钩子");
  demoLog("等待进程 B 被 compose 重启 …");
  if (!(await waitPeer(peer))) demoLog("进程 B 未在预期时间内恢复");
  await setHook("", peer).catch(() => {});
  const after = await callApi(`/api/tasks/${tid}`);
  demoLog(`重启后读库：修订 #${after.revision}，人工选择 ${demoPins(after)}`);
  let expStatus = 0;
  try {
    expStatus = (await fetch(
      `${peer}/api/tasks/${tid}/export?revision=${rev}`)).status;
  } catch {
    expStatus = 0;
  }
  demoLog(`用旧修订 #${rev} 导出：HTTP ${expStatus}`);
  if (when === "before-commit") {
    const good = after.revision === rev && !demoRow(after, 2).pinned;
    toast(good ? "提交前退出：旧状态保留，旧修订不能导出新 UTC"
               : "状态不符合预期，请查看日志");
  } else {
    const good = after.revision === rev + 1 && demoRow(after, 2).pinned;
    toast(good ? "提交后退出：重启后仍是新版" : "状态不符合预期，请查看日志");
  }
}

async function demoReadRace(peer) {
  demoLog("④ 查询中换时区：A 读任务后暂停，B 换时区；A 的响应不得拼接");
  const st = await demoUpload();
  const tid = st.id;
  let cur = await callApi(`/api/tasks/${tid}/selections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      row_id: demoRow(st, 2).id, utc: "2024-11-03T05:00:00+00:00", revision: st.revision,
    }),
  });
  const rev = cur.revision;
  await setHook("read");
  const pending = callApi(`/api/tasks/${tid}`);
  await new Promise((r) => setTimeout(r, 250));
  const tz = await callApi(`${peer}/api/tasks/${tid}/timezone`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone: "Asia/Shanghai", revision: rev }),
  });
  const got = await pending;
  await setHook("");
  demoLog(`并发查询返回：修订 #${got.revision}，时区 ${got.timezone}，人工选择 ${demoPins(got)}`);
  demoLog(`换时区返回：修订 #${tz.revision}，时区 ${tz.timezone}，人工选择 ${demoPins(tz)}`);
  const latest = await callApi(`/api/tasks/${tid}`);
  demoLog(`库内最新：修订 #${latest.revision}，时区 ${latest.timezone}`);
  const good = got.revision === rev && got.timezone === "America/New_York";
  toast(good ? "查询响应来自同一份已提交快照，没有拼接" : "出现拼接，请查看日志");
}

document.querySelectorAll(".demo-run").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!demoInfo || !demoInfo.peer) {
      return toast("演示环境未配置另一共库进程地址（APP_PEER_URL）");
    }
    btn.disabled = true;
    $("demo-log").textContent = "";
    try {
      const kind = btn.dataset.demo;
      if (kind === "race") await demoRace(demoInfo.peer);
      else if (kind === "read-race") await demoReadRace(demoInfo.peer);
      else await demoCrash(
        demoInfo.peer,
        kind === "crash-before" ? "before-commit" : "after-commit"
      );
    } catch (e) {
      demoLog("演示中断：" + e.message);
      toast("演示中断：" + e.message);
    } finally {
      btn.disabled = false;
      showDemo("");
    }
  });
});
