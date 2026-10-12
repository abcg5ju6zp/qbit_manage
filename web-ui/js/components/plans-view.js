/**
 * qBit Manage Web UI - Execution Plans component
 * Lists persisted preview plans and renders their per-action detail view with
 * precondition validation, explicit confirmation, skip and crash resume.
 */

import { showToast } from '../utils/toast.js';

const STATUS_BADGE = {
    pending: 'plan-badge plan-badge-pending',
    running: 'plan-badge plan-badge-running',
    completed: 'plan-badge plan-badge-applied',
    completed_with_failures: 'plan-badge plan-badge-failed',
    invalid: 'plan-badge plan-badge-stale'
};

const ACTION_STATUS_BADGE = {
    pending: 'plan-badge plan-badge-pending',
    applied: 'plan-badge plan-badge-applied',
    skipped: 'plan-badge plan-badge-skipped',
    stale: 'plan-badge plan-badge-stale',
    failed: 'plan-badge plan-badge-failed',
    uncertain: 'plan-badge plan-badge-uncertain'
};

const TYPE_LABELS = {
    tags_add: 'Add tags',
    tags_remove: 'Remove tags',
    category_set: 'Set category',
    category_enable_tmm: 'Enable auto TMM',
    resume: 'Resume torrent',
    recheck: 'Force recheck',
    nohl_add: 'Add no-hardlinks tag',
    nohl_remove: 'Remove no-hardlinks tag',
    tracker_error_add: 'Add tracker-error tag',
    tracker_error_remove: 'Remove tracker-error tag',
    unreg_delete: 'Delete unregistered torrent',
    share_tag_add: 'Add share-limit tag',
    share_tag_remove: 'Remove share-limit tag',
    set_share_limits: 'Set share limits',
    set_upload_limit: 'Set upload limit',
    share_resume: 'Resume torrent',
    share_cleanup_delete: 'Delete torrent (share limit)',
    tag_definition_delete: 'Delete tag definition',
    orphan_file_delete: 'Delete orphaned file',
    orphan_file_move: 'Move orphaned file',
    orphan_rmdir: 'Remove empty directory',
    pause: 'Pause torrent'
};

class PlansView {
    constructor({ container, api }) {
        this.container = container;
        this.api = api || window.app?.api;
        this.currentPlanId = null;
        this.pollTimer = null;
        this.render();
    }

    render() {
        this.container.innerHTML = `
            <div class="plans-view">
                <div class="plans-header">
                    <h2>Execution Plans</h2>
                    <div class="plans-actions">
                        <button type="button" class="btn btn-outline" id="plans-refresh-btn">Refresh</button>
                    </div>
                </div>
                <div id="plans-body"><p class="plans-empty">Loading plans…</p></div>
            </div>`;
        this.container.querySelector('#plans-refresh-btn').addEventListener('click', () => {
            if (this.currentPlanId) this.openPlan(this.currentPlanId, true);
            else this.showList();
        });
    }

    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    }

    async showList() {
        this.stopPolling();
        this.currentPlanId = null;
        const body = this.container.querySelector('#plans-body');
        body.innerHTML = '<p class="plans-empty">Loading plans…</p>';
        try {
            const response = await this.api.listPlans();
            const plans = response.plans || [];
            if (!plans.length) {
                body.innerHTML =
                    '<p class="plans-empty">No execution plans yet. Open Commands → Run Commands and choose “Create Plan” after selecting commands.</p>';
                return;
            }
            body.innerHTML = `
                <div class="table-wrap">
                <table class="plan-table">
                    <thead><tr>
                        <th>Created</th><th>Config</th><th>Commands</th><th>Scope</th>
                        <th>Status</th><th>Actions</th>
                    </tr></thead>
                    <tbody>
                        ${plans.map(p => `
                            <tr data-plan-id="${p.plan_id}" class="plan-row">
                                <td data-label="Created"><span class="plan-time">${this._time(p.created_at)}</span></td>
                                <td data-label="Config" class="plan-cell-name">${this._esc(p.config_file)}</td>
                                <td data-label="Commands">${(p.commands || []).map(c => `<span class="plan-chip">${this._esc(c)}</span>`).join(' ')}</td>
                                <td data-label="Scope">${(p.hashes || []).length ? `${p.hashes.length} hash(es)` : 'all torrents'}</td>
                                <td data-label="Status"><span class="${STATUS_BADGE[p.status] || STATUS_BADGE.pending}">${this._statusLabel(p.status)}</span></td>
                                <td data-label="Actions">${this._counts(p.result_counts)}</td>
                            </tr>`).join('')}
                    </tbody>
                </table>
                </div>`;
            body.querySelectorAll('.plan-row').forEach(row => {
                row.addEventListener('click', () => this.openPlan(row.dataset.planId));
            });
        } catch (error) {
            body.innerHTML = `<p class="plans-empty">Failed to load plans: ${this._esc(error.message)}</p>`;
        }
    }

    async openPlan(planId, silent = false) {
        this.stopPolling();
        this.currentPlanId = planId;
        const body = this.container.querySelector('#plans-body');
        if (!silent) body.innerHTML = '<p class="plans-empty">Loading plan…</p>';
        try {
            const plan = await this.api.getPlan(planId);
            this.renderDetail(plan);
            if (plan.status === 'running') this.startPolling();
        } catch (error) {
            body.innerHTML = `<p class="plans-empty">Failed to load plan: ${this._esc(error.message)}</p>`;
        }
    }

    startPolling() {
        this.stopPolling();
        this.pollTimer = setInterval(async () => {
            try {
                const plan = await this.api.getPlan(this.currentPlanId);
                this.renderDetail(plan);
                if (plan.status !== 'running') this.stopPolling();
            } catch (e) {
                this.stopPolling();
            }
        }, 2000);
    }

    renderDetail(plan) {
        const body = this.container.querySelector('#plans-body');
        const pendingActions = (plan.actions || []).filter(a => a.result === 'pending');
        const valid = plan.valid !== false && plan.status !== 'invalid';
        const confirmable = valid && pendingActions.length > 0 && plan.status !== 'running';
        const resumable = plan.status === 'running' ||
            (plan.status === 'completed_with_failures' && pendingActions.length > 0);

        body.innerHTML = `
            <div class="plan-detail">
                <div class="plans-header">
                    <div class="plan-detail-title">
                        <button type="button" class="btn btn-outline" id="plan-back-btn">← All plans</button>
                        <h2 title="${this._esc(plan.plan_id)}">Plan ${this._esc(plan.plan_id.slice(0, 8))}</h2>
                        <span class="${STATUS_BADGE[plan.status] || STATUS_BADGE.pending}">${this._statusLabel(plan.status)}</span>
                    </div>
                    <div class="plans-actions">
                        <button type="button" class="btn btn-outline" id="plan-revalidate-btn">Revalidate</button>
                        <button type="button" class="btn btn-secondary" id="plan-resume-btn" ${resumable ? '' : 'disabled'}>Resume pending</button>
                        <button type="button" class="btn btn-primary" id="plan-confirm-btn" ${confirmable ? '' : 'disabled'}>
                            Confirm &amp; execute (${pendingActions.length} pending)
                        </button>
                        <button type="button" class="btn btn-outline btn-danger-outline" id="plan-delete-btn"
                            ${plan.status === 'running' ? 'disabled' : ''}>Discard</button>
                    </div>
                </div>

                <div class="plan-meta">
                    <div><strong>Config:</strong> ${this._esc(plan.config_file)}</div>
                    <div><strong>Commands:</strong> ${(plan.commands || []).map(c => `<span class="plan-chip">${this._esc(c)}</span>`).join(' ')}</div>
                    <div><strong>Scope:</strong> ${(plan.hashes || []).length ? this._esc(plan.hashes.join(', ')) : 'all torrents'}</div>
                    <div><strong>Created:</strong> ${this._time(plan.created_at)} ${plan.heartbeat_at ? `· updated ${this._time(plan.updated_at)}` : ''}</div>
                </div>

                ${valid ? '<div class="plan-alert plan-alert-ok">Plan is confirmable: frozen config matches the file on disk. Per-action preconditions are re-checked at execution.</div>' : ''}
                ${(plan.invalid_reasons || []).length ? `
                    <div class="plan-alert plan-alert-error">
                        <strong>Plan cannot be executed:</strong>
                        <ul>${plan.invalid_reasons.map(r => `<li>${this._esc(r)}</li>`).join('')}</ul>
                    </div>` : ''}

                ${this._summaryHtml(plan)}

                <div class="table-wrap">
                <table class="plan-table plan-actions-table">
                    <thead><tr>
                        <th class="col-skip">Skip</th><th>#</th><th>Action</th><th>Target</th><th>Reason</th>
                        <th>Impact</th><th>Result</th>
                    </tr></thead>
                    <tbody>
                        ${(plan.actions || []).map(a => this._actionRow(a)).join('')}
                    </tbody>
                </table>
                </div>
                <p class="plan-help">Check “Skip” for pending actions you do not want executed, then press Confirm.
                    Stale actions never produce side effects. Destructive actions are reconciled against qBittorrent and disk after execution.</p>
            </div>`;

        body.querySelector('#plan-back-btn').addEventListener('click', () => this.showList());
        body.querySelector('#plan-revalidate-btn').addEventListener('click', () => this.revalidate());
        body.querySelector('#plan-delete-btn').addEventListener('click', () => this.discard(plan));
        body.querySelector('#plan-confirm-btn').addEventListener('click', () => this.confirm(plan));
        body.querySelector('#plan-resume-btn').addEventListener('click', () => this.resume(plan));
    }

    _actionRow(a) {
        const pending = a.result === 'pending';
        const target = this._targetText(a);
        const preflight = a.preflight && !a.preflight.valid
            ? `<div class="plan-preflight plan-preflight-stale">Preview now: ${this._esc(a.preflight.reason || 'stale')}</div>`
            : (a.preflight && a.preflight.valid
                ? '<div class="plan-preflight plan-preflight-ok">Preview now: still valid</div>'
                : '');
        return `
            <tr class="plan-action-row">
                <td data-label="Skip" class="col-skip">
                    ${pending ? `<input type="checkbox" class="plan-skip-checkbox" data-action-id="${a.id}" aria-label="Skip action ${a.id}">` : ''}
                </td>
                <td data-label="#">${a.id}</td>
                <td data-label="Action">
                    <div class="plan-action-name">${this._esc(TYPE_LABELS[a.type] || a.type)}</div>
                    <div class="plan-action-cmd">${this._esc(a.command)}</div>
                </td>
                <td data-label="Target" class="plan-cell-name">${target}</td>
                <td data-label="Reason" class="plan-cell-reason">${this._esc(a.reason || '')}${preflight}</td>
                <td data-label="Impact">
                    ${a.modifies ? '<span class="plan-impact plan-impact-modifies">modifies</span>' : ''}
                    ${a.deletes ? '<span class="plan-impact plan-impact-deletes">deletes</span>' : ''}
                </td>
                <td data-label="Result">
                    <span class="${ACTION_STATUS_BADGE[a.result] || ACTION_STATUS_BADGE.pending}">${this._esc(a.result)}</span>
                    ${a.message ? `<div class="plan-result-message">${this._esc(a.message)}</div>` : ''}
                </td>
            </tr>`;
    }

    _targetText(a) {
        const parts = [];
        if (a.target && a.target.name) parts.push(a.target.name);
        if (a.target && a.target.hash) parts.push(`<code>${this._esc(a.target.hash.slice(0, 12))}…</code>`);
        if (a.target && Array.isArray(a.target.paths) && a.target.paths.length) {
            parts.push(a.target.paths.map(p => `<div class="plan-path" title="${this._esc(p)}">${this._esc(p)}</div>`).join(''));
        }
        if (a.params && (a.params.src || a.params.dir)) {
            parts.push(`<div class="plan-path" title="${this._esc(a.params.src || a.params.dir)}">${this._esc(a.params.src || a.params.dir)}</div>`);
        }
        return parts.join('<br>') || '—';
    }

    _summaryHtml(plan) {
        const s = plan.summary || {};
        const entries = [
            ['applied', s.applied || 0],
            ['skipped', s.skipped || 0],
            ['stale', s.stale || 0],
            ['failed', s.failed || 0],
            ['uncertain', s.uncertain || 0]
        ];
        if (!plan.status || plan.status === 'pending') return '';
        return `<div class="plan-summary">${entries
            .filter(([, n]) => n > 0)
            .map(([k, n]) => `<span class="${ACTION_STATUS_BADGE[k]}">${k}: ${n}</span>`)
            .join(' ')}</div>`;
    }

    _counts(counts) {
        if (!counts) return '';
        return Object.entries(counts)
            .filter(([, n]) => n > 0)
            .map(([k, n]) => `<span class="plan-count">${this._esc(k)} ${n}</span>`)
            .join(' ');
    }

    async revalidate() {
        try {
            showToast('Revalidating preconditions…', 'success');
            const plan = await this.api.getPlan(this.currentPlanId, true);
            this.renderDetail(plan);
        } catch (error) {
            showToast(`Revalidation failed: ${error.message}`, 'error');
        }
    }

    _selectedSkipIds() {
        return Array.from(this.container.querySelectorAll('.plan-skip-checkbox:checked')).map(cb => cb.dataset.actionId);
    }

    async confirm(plan) {
        const skipIds = this._selectedSkipIds();
        const hasDeletes = (plan.actions || []).some(a => a.result === 'pending' && a.deletes && !skipIds.includes(a.id));
        const message = hasDeletes
            ? `Execute plan ${plan.plan_id.slice(0, 8)}? Some pending actions DELETE torrents or files. This cannot be undone.${skipIds.length ? `\n${skipIds.length} action(s) will be skipped.` : ''}`
            : `Execute plan ${plan.plan_id.slice(0, 8)}? ${skipIds.length ? `${skipIds.length} action(s) will be skipped.` : ''}`;
        if (!window.confirm(message)) return;
        try {
            const result = await this.api.confirmPlan(plan.plan_id, skipIds);
            showToast(`Plan finished: ${JSON.stringify(result.summary || {})}`, 'success');
            await this.openPlan(plan.plan_id, true);
            this.startPolling();
        } catch (error) {
            showToast(`Confirmation failed: ${error.message}`, 'error');
            await this.openPlan(plan.plan_id, true);
        }
    }

    async resume(plan) {
        try {
            await this.api.resumePlan(plan.plan_id);
            showToast('Resuming pending actions…', 'success');
            await this.openPlan(plan.plan_id, true);
            this.startPolling();
        } catch (error) {
            showToast(`Resume failed: ${error.message}`, 'error');
            await this.openPlan(plan.plan_id, true);
        }
    }

    async discard(plan) {
        if (!window.confirm(`Discard plan ${plan.plan_id.slice(0, 8)}? Its stored record will be deleted.`)) return;
        try {
            await this.api.deletePlan(plan.plan_id);
            showToast('Plan discarded', 'success');
            this.showList();
        } catch (error) {
            showToast(`Delete failed: ${error.message}`, 'error');
        }
    }

    _statusLabel(status) {
        return (status || 'pending').replace(/_/g, ' ');
    }

    _time(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        return isNaN(d.getTime()) ? iso : d.toLocaleString();
    }

    _esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[ch]));
    }
}

export { PlansView };
