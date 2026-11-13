/* global fetch, location, history, Blob, URL, confirm, alert */
"use strict";

let task = null; // 当前任务状态（含所见修订号）

const $ = (id) => document.getElementById(id);

function toast(msg, ms = 4200) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), ms);
}

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const ct = resp.headers.get("content-type") || "";
  const data = ct.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const err = new Error(typeof data === "object" ? data.detail : data);
    err.status = resp.status;
    throw err;
  }
  return data;
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
      `/api/tasks/${task.id}/export?revision=${task.revision}`
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
})();
