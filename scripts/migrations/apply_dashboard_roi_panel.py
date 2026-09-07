#!/usr/bin/env python3
"""
Adds a prominent "Downtime Cost Avoided" panel to the dashboard, right
below the existing 5-tile KPI row -- so the business-ROI number is the
first thing a non-DBA judge sees after the header, not something buried in
a per-engine detail panel further down.

Run from the project root:
    python3 apply_dashboard_roi_panel.py

Targets gcp_deploy/services/orchestrator/static/dashboard.html. Two
anchor-verified edits: the CSS block (inserted right before the closing
</style>) and the HTML+JS panel (inserted right after the existing
`.tiles` div, before `.board`). Backs up to dashboard.html.bak.preroi first.

What it shows: Cost Avoided = (Human MTTA − NEXORA's measured onset-to-kill)
x Cost/Minute, using this project's own already-measured per-engine numbers
(Oracle 63s, MySQL 61s -- see the architecture blueprint's detection->action
table) rather than a single blended average, since Tier 1 auto-kills and
Tier 3 human-approved actions resolve on very different timescales by
design. The 15-30 minute human-MTTA range is disclosed inline as an
industry-general benchmark, NOT a number verified for this team -- exactly
the same disclosure already given in the overview deck's business-impact
section, kept consistent here rather than silently dropped for a
better-looking dashboard number. Cost/minute is a plain editable input
(this project has no real figure for it) that persists locally via
localStorage so a judge/reviewer doesn't have to re-type it on every
reload; nothing about that value is sent anywhere -- it's dashboard-local,
same trust boundary as everything else this static page renders client-side.
"""
path = "gcp_deploy/services/orchestrator/static/dashboard.html"
with open(path) as f:
    content = f.read()
original = content


def verify_once(c, anchor, label):
    n = c.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT [{label}]: expected exactly 1 match, found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )


css_anchor = "</style>"
verify_once(content, css_anchor, "css close tag")

css_addition = '''
  /* ---- ROI panel (Cost Avoided) ---- */
  .roi-panel {
    flex: 0 0 auto; background: var(--surface-1); border: 1px solid var(--border-strong);
    border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow-card);
    display: flex; flex-wrap: wrap; align-items: center; gap: 18px; margin-top: 10px;
  }
  .roi-panel .roi-headline { flex: 1 1 260px; min-width: 220px; }
  .roi-panel .roi-label {
    font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-secondary); margin-bottom: 4px;
  }
  .roi-panel .roi-value {
    font-size: 30px; font-weight: 800; letter-spacing: -0.01em; color: var(--good);
    font-variant-numeric: tabular-nums; line-height: 1.05;
  }
  .roi-panel .roi-formula {
    font-size: 11px; color: var(--text-muted); margin-top: 5px; font-variant-numeric: tabular-nums;
  }
  .roi-panel .roi-controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
  .roi-panel .roi-field { display: flex; flex-direction: column; gap: 3px; }
  .roi-panel .roi-field label {
    font-size: 10px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .roi-panel .roi-field select, .roi-panel .roi-field input {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 7px;
    color: var(--text-primary); font-size: 12.5px; padding: 6px 9px; font-variant-numeric: tabular-nums;
    width: 128px;
  }
  .roi-panel .roi-disclosure {
    flex: 1 1 100%; font-size: 10.5px; color: var(--text-muted); border-top: 1px solid var(--border);
    padding-top: 8px; margin-top: 2px; line-height: 1.5;
  }
</style>'''

content = content.replace(css_anchor, css_addition, 1)

html_anchor = '''      </div>
      <div class="board cols-3">'''
verify_once(content, html_anchor, "tiles/board boundary")

html_addition = '''      </div>
      <div class="roi-panel" id="roi-panel">
        <div class="roi-headline">
          <div class="roi-label">Downtime cost avoided (this incident class)</div>
          <div class="roi-value" id="roi-value">--</div>
          <div class="roi-formula" id="roi-formula">(Human MTTA &minus; NEXORA onset-to-kill) &times; cost/min</div>
        </div>
        <div class="roi-controls">
          <div class="roi-field">
            <label for="roi-engine">Incident class</label>
            <select id="roi-engine">
              <option value="63" data-label="Oracle blocking-session kill">Oracle &middot; 63s onset-to-kill</option>
              <option value="61" data-label="MySQL blocking-session kill">MySQL &middot; 61s onset-to-kill</option>
            </select>
          </div>
          <div class="roi-field">
            <label for="roi-mtta">Human MTTA (min)</label>
            <select id="roi-mtta">
              <option value="15">15 (fast on-call)</option>
              <option value="22.5" selected>22.5 (midpoint)</option>
              <option value="30">30 (typical)</option>
            </select>
          </div>
          <div class="roi-field">
            <label for="roi-cost">Cost / minute ($)</label>
            <input type="number" id="roi-cost" min="0" step="1" value="50" />
          </div>
        </div>
        <div class="roi-disclosure">
          NEXORA's onset-to-kill figures are measured directly from this deployment's own audit trail
          (see the architecture blueprint's detection&rarr;action table). The 15&ndash;30 minute human-MTTA
          range is a commonly-cited industry benchmark for on-call response, not a number verified for
          any specific team &mdash; replace it with your own real figure once you have one. Cost/minute is
          your own estimate; it's stored only in this browser (not sent anywhere) so it persists across
          reloads here.
        </div>
      </div>
      <div class="board cols-3">'''

content = content.replace(html_anchor, html_addition, 1)

js_anchor = "</body>"
verify_once(content, js_anchor, "body close tag")

js_addition = '''<script>
(function () {
  var engineSel = document.getElementById('roi-engine');
  var mttaSel = document.getElementById('roi-mtta');
  var costInput = document.getElementById('roi-cost');
  var valueEl = document.getElementById('roi-value');
  var formulaEl = document.getElementById('roi-formula');
  if (!engineSel || !mttaSel || !costInput || !valueEl) return;

  try {
    var savedCost = localStorage.getItem('nexora_roi_cost_per_min');
    if (savedCost) costInput.value = savedCost;
  } catch (e) { /* localStorage unavailable -- fall back to the default value silently */ }

  function fmtUsd(n) {
    return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
  }

  function recompute() {
    var onsetSec = parseFloat(engineSel.value);
    var mttaMin = parseFloat(mttaSel.value);
    var costPerMin = parseFloat(costInput.value);
    if (isNaN(onsetSec) || isNaN(mttaMin) || isNaN(costPerMin) || costPerMin < 0) {
      valueEl.textContent = '--';
      return;
    }
    var minutesSaved = mttaMin - (onsetSec / 60);
    if (minutesSaved <= 0) {
      valueEl.textContent = fmtUsd(0);
      formulaEl.textContent = 'NEXORA onset-to-kill already exceeds the selected human MTTA -- no avoided cost at this setting.';
      return;
    }
    var avoided = minutesSaved * costPerMin;
    valueEl.textContent = fmtUsd(avoided);
    var label = engineSel.options[engineSel.selectedIndex].getAttribute('data-label') || '';
    formulaEl.textContent = '(' + mttaMin + 'm human MTTA − ' + (onsetSec / 60).toFixed(2) +
      'm ' + label + ') × ' + fmtUsd(costPerMin) + '/min ≈ ' + minutesSaved.toFixed(2) + 'm saved';
    try { localStorage.setItem('nexora_roi_cost_per_min', String(costPerMin)); } catch (e) { /* ignore */ }
  }

  engineSel.addEventListener('change', recompute);
  mttaSel.addEventListener('change', recompute);
  costInput.addEventListener('input', recompute);
  recompute();
})();
</script>
</body>'''

content = content.replace(js_anchor, js_addition, 1)

backup_path = path + ".bak.preroi"
with open(backup_path, "w") as f:
    f.write(original)
with open(path, "w") as f:
    f.write(content)

print(f"OK: patched {path} (backup at {backup_path})")
print("Open the dashboard and confirm the panel renders and the inputs recompute live.")
