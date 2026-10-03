function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Render a millisecond duration at a readable scale: "84 ms", "55.2 s", "1m 55s".
function formatDuration(ms) {
    const value = Number(ms);
    if (!Number.isFinite(value) || value <= 0) return '0 ms';
    if (value < 1000) return `${Math.round(value)} ms`;
    if (value < 60000) return `${(value / 1000).toFixed(1)} s`;
    const totalSeconds = Math.round(value / 1000);
    return `${Math.floor(totalSeconds / 60)}m ${totalSeconds % 60}s`;
}

const TIER_LABELS = {
    tier1_fast_tls: 'Fast TLS',
    tier2_cache: 'Cookie cache',
    tier3_stealth_browser: 'Browser',
    tier4_fallback_proxy: 'Proxy',
    failed: 'Failed'
};

function tierLabel(tier) {
    return TIER_LABELS[tier] || tier || '—';
}

document.addEventListener('DOMContentLoaded', () => {
    // Mobile navigation & sidebar drawer controls
    const mobileMenuBtn = document.getElementById('btn-mobile-menu');
    const sidebarCloseBtn = document.getElementById('btn-sidebar-close');
    const sidebarBackdrop = document.getElementById('sidebar-backdrop');

    function openMobileMenu() {
        document.body.classList.add('sidebar-open');
        if (mobileMenuBtn) {
            mobileMenuBtn.setAttribute('aria-expanded', 'true');
        }
    }

    function closeMobileMenu() {
        document.body.classList.remove('sidebar-open');
        if (mobileMenuBtn) {
            mobileMenuBtn.setAttribute('aria-expanded', 'false');
        }
    }

    if (mobileMenuBtn) {
        mobileMenuBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (document.body.classList.contains('sidebar-open')) {
                closeMobileMenu();
            } else {
                openMobileMenu();
            }
        });
    }

    if (sidebarCloseBtn) {
        sidebarCloseBtn.addEventListener('click', closeMobileMenu);
    }

    if (sidebarBackdrop) {
        sidebarBackdrop.addEventListener('click', closeMobileMenu);
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
            closeMobileMenu();
        }
    });

    // Unified tabs (Sidebar & Mobile Bottom Nav)
    const allNavItems = document.querySelectorAll('.nav-item, .mobile-nav-item');
    const tabPages = document.querySelectorAll('.tab-page');
    const tabTitle = document.getElementById('current-tab-title');

    function switchTab(tabName) {
        allNavItems.forEach(n => {
            if (n.getAttribute('data-tab') === tabName) {
                n.classList.add('active');
            } else {
                n.classList.remove('active');
            }
        });

        tabPages.forEach(p => p.classList.remove('active'));
        const targetPage = document.getElementById(`tab-${tabName}`);
        if (targetPage) targetPage.classList.add('active');

        // Update header title based on active sidebar item
        const matchingSidebarItem = document.querySelector(`.sidebar .nav-item[data-tab="${tabName}"] span`);
        if (matchingSidebarItem && tabTitle) {
            tabTitle.textContent = matchingSidebarItem.textContent;
        }

        if (tabName === 'cookies') {
            fetchCookies();
        } else if (tabName === 'history') {
            fetchHistory();
        }

        closeMobileMenu();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    allNavItems.forEach(item => {
        item.addEventListener('click', () => {
            const tabName = item.getAttribute('data-tab');
            if (tabName) switchTab(tabName);
        });
    });

    // Show the POST body field only for POST
    const testMethodSelect = document.getElementById('test-method');
    const postDataContainer = document.getElementById('post-data-container');
    if (testMethodSelect && postDataContainer) {
        testMethodSelect.addEventListener('change', () => {
            if (testMethodSelect.value === 'POST') {
                postDataContainer.classList.remove('hidden');
            } else {
                postDataContainer.classList.add('hidden');
            }
        });
    }

    async function fetchStats() {
        try {
            const res = await fetch('/api/stats');
            if (!res.ok) return;
            const data = await res.json();

            document.getElementById('val-total-reqs').textContent = data.total_requests || 0;
            document.getElementById('foot-failed').textContent = `${data.failed_requests || 0} failed`;
            document.getElementById('val-fast-rate').textContent = (data.fast_hit_rate_pct || 0) + '%';
            document.getElementById('val-fast-hits').textContent = (data.tier1_fast_tls_hits || 0) + (data.tier2_cache_hits || 0);
            const [fastValue, fastUnit] = formatDuration(data.avg_fast_ms).split(' ');
            document.getElementById('val-fast-ms').innerHTML = fastUnit
                ? `${escapeHtml(fastValue)}<span class="unit">${escapeHtml(fastUnit)}</span>`
                : escapeHtml(fastValue);
            const browserAvg = data.avg_browser_ms ? formatDuration(data.avg_browser_ms) : '—';
            document.getElementById('val-browser-ms').textContent = browserAvg;
            if (document.getElementById('val-tier3-avg')) document.getElementById('val-tier3-avg').textContent = browserAvg;
            if (document.getElementById('val-browser-measured')) document.getElementById('val-browser-measured').textContent = browserAvg;
            document.getElementById('val-ram').innerHTML = `${data.ram_usage_mb || 0}<span class="unit">MB</span>`;
            document.getElementById('val-cpu').textContent = (data.cpu_usage_pct || 0) + '%';

            if (document.getElementById('val-tier1-hits')) {
                document.getElementById('val-tier1-hits').textContent = data.tier1_fast_tls_hits || 0;
            }
            if (document.getElementById('val-tier2-hits')) {
                document.getElementById('val-tier2-hits').textContent = data.tier2_cache_hits || 0;
            }
            if (document.getElementById('val-tier3-hits')) {
                document.getElementById('val-tier3-hits').textContent = data.tier3_stealth_browser_solves || 0;
            }
            if (document.getElementById('val-tier4-hits')) {
                document.getElementById('val-tier4-hits').textContent = data.tier4_fallback_proxy_hits || 0;
            }

            const pool = data.browser_pool || {};
            if (document.getElementById('val-pool-busy')) document.getElementById('val-pool-busy').textContent = pool.busy || 0;
            if (document.getElementById('val-pool-size')) document.getElementById('val-pool-size').textContent = pool.pool_size || 0;
            if (document.getElementById('val-pool-idle')) document.getElementById('val-pool-idle').textContent = pool.idle || 0;
            if (document.getElementById('val-pool-recycles')) document.getElementById('val-pool-recycles').textContent = pool.recycles_total || 0;
            if (document.getElementById('val-browser-crashes')) document.getElementById('val-browser-crashes').textContent = pool.crashes_total || 0;
            if (document.getElementById('val-queue-wait')) document.getElementById('val-queue-wait').textContent = Math.round((pool.avg_queue_wait_seconds || 0) * 1000);
            document.getElementById('val-queue-depth').textContent = pool.queue_depth || 0;
            document.getElementById('val-queue-oldest').textContent = formatDuration((pool.oldest_queue_wait_seconds || 0) * 1000);
            document.getElementById('val-checkout-oldest').textContent = formatDuration((pool.oldest_checkout_seconds || 0) * 1000);
            if (document.getElementById('val-cache-hit-ratio')) document.getElementById('val-cache-hit-ratio').textContent = (data.cache_hit_ratio_pct || 0) + '%';
            if (document.getElementById('val-cache-lookups')) {
                const lookups = (data.cookie_cache_lookup_hits || 0) + (data.cookie_cache_lookup_misses || 0);
                document.getElementById('val-cache-lookups').textContent = lookups;
            }
            if (document.getElementById('val-cache-served')) document.getElementById('val-cache-served').textContent = data.tier2_cache_hits || 0;
            if (document.getElementById('val-timeouts')) document.getElementById('val-timeouts').textContent = data.timeouts_total || 0;

            const workerLabel = data.worker_auto_tuned 
                ? `${data.max_workers || 4} Workers (Auto)` 
                : `${data.max_workers || 3} Workers`;
            document.getElementById('val-max-workers').textContent = workerLabel;
            document.getElementById('val-host-hardware').textContent = `${data.total_cpu_cores || 4} Cores / ${data.total_ram_gb || 8}GB RAM`;
            document.getElementById('val-cached-domains').textContent = `${data.cached_domains_count || 0} Domains`;

            if (data.cache_backend && document.getElementById('val-cache-backend')) {
                document.getElementById('val-cache-backend').textContent = data.cache_backend;
            }

            if (data.stealth_engine && document.getElementById('val-stealth-engine')) {
                document.getElementById('val-stealth-engine').textContent = data.stealth_engine;
            }
            if (data.version) {
                if (document.getElementById('val-version-tag')) {
                    document.getElementById('val-version-tag').textContent = data.version;
                }
                if (document.getElementById('val-version-tag-mobile')) {
                    document.getElementById('val-version-tag-mobile').textContent = data.version;
                }
            }
            if (data.tls_impersonation) {
                const imp = String(data.tls_impersonation).toLowerCase();
                let browserName = 'Firefox';
                if (imp.includes('chrome')) browserName = 'Chrome';
                else if (imp.includes('firefox') || imp.includes('ff')) browserName = 'Firefox';
                else if (imp.includes('edge')) browserName = 'Edge';
                else if (imp.includes('safari')) browserName = 'Safari';

                const versionMatch = imp.match(/(\d+)/);
                const ver = versionMatch ? versionMatch[1] : 'Latest';
                const label = `${browserName} ${ver} JA3`;
                document.getElementById('val-tls-impersonation').textContent = label;
            }
        } catch (e) {
            console.error('Stats poll error:', e);
        }
    }

    fetchStats();
    setInterval(fetchStats, 3000);

    document.getElementById('btn-refresh-stats').addEventListener('click', fetchStats);

    let historyRequest = 0;
    async function fetchHistory() {
        const requestId = ++historyRequest;
        const domain = document.getElementById('history-domain').value.trim();
        const outcome = document.getElementById('history-outcome').value;
        const tier = document.getElementById('history-tier').value;
        const hours = document.getElementById('history-hours').value;
        const params = new URLSearchParams();
        if (domain) params.set('domain', domain);
        if (outcome) params.set('outcome', outcome);
        if (tier) params.set('tier', tier);
        params.set('hours', hours);
        const error = document.getElementById('history-error');
        try {
            const [res, summaryRes] = await Promise.all([
                fetch(`/api/history?limit=100&${params}`),
                fetch(`/api/history/summary?${params}`)
            ]);
            if (!res.ok || !summaryRes.ok) throw new Error(`History unavailable (HTTP ${res.ok ? summaryRes.status : res.status})`);
            const rows = (await res.json()).requests || [];
            const summary = await summaryRes.json();
            if (requestId !== historyRequest) return;
            error.hidden = true;
            document.getElementById('history-total').textContent = summary.total;
            document.getElementById('history-period-label').textContent = `Requests · ${hours === '168' ? '7 days' : `${hours} hour${hours === '1' ? '' : 's'}`}`;
            document.getElementById('history-failed').textContent = summary.failed;
            document.getElementById('history-p95').textContent = summary.p95_ms === null ? '—' : formatDuration(summary.p95_ms);
            const body = document.getElementById('history-rows');
            body.replaceChildren();
            for (const row of rows) {
                const tr = document.createElement('tr');
                const values = [new Date(row.timestamp * 1000).toLocaleString(), row.domain, tierLabel(row.tier),
                    row.http_status ? `${row.outcome} (${row.http_status})` : row.outcome,
                    formatDuration(row.duration_ms), row.failure_type || row.challenge || '—'];
                for (const value of values) {
                    const td = document.createElement('td');
                    td.textContent = value;
                    tr.appendChild(td);
                }
                body.appendChild(tr);
            }
            if (!rows.length) body.innerHTML = '<tr><td colspan="6">No matching requests.</td></tr>';
            const trend = document.getElementById('history-trend');
            trend.replaceChildren();
            const timeBuckets = summary.hours || [];
            const peak = Math.max(1, ...timeBuckets.map(hour => hour.success + hour.failed));
            for (const hour of timeBuckets) {
                const bar = document.createElement('div');
                bar.className = 'history-bar';
                const count = hour.success + hour.failed;
                bar.title = `${new Date(hour.timestamp * 1000).toLocaleString()}: ${hour.success} success, ${hour.failed} failed`;
                bar.setAttribute('aria-label', bar.title);
                for (const [outcome, value] of [['success', hour.success], ['failed', hour.failed]]) {
                    if (!value) continue;
                    const segment = document.createElement('div');
                    segment.className = `history-bar-${outcome}`;
                    segment.style.height = `${value / peak * 100}%`;
                    bar.appendChild(segment);
                }
                if (!count) bar.classList.add('history-bar-empty');
                trend.appendChild(bar);
            }
            trend.setAttribute('aria-label', `Request volume over the past ${summary.window_hours} hours: ${summary.total} requests, ${summary.failed} failed`);
            const renderBreakdown = (elementId, entries, fallback) => {
                const container = document.getElementById(elementId);
                container.replaceChildren();
                if (!entries.length) { container.textContent = fallback; return; }
                for (const [label, count] of entries) {
                    const line = document.createElement('p');
                    const name = document.createElement('span');
                    const total = document.createElement('strong');
                    name.textContent = label;
                    total.textContent = count;
                    line.append(name, total);
                    container.appendChild(line);
                }
            };
            renderBreakdown('history-tier-counts', Object.entries(summary.tier_counts || {}).sort((a, b) => b[1] - a[1]).map(([tier, count]) => [tierLabel(tier), count]), 'No requests yet.');
            renderBreakdown('history-failed-domains', summary.top_failed_domains || [], 'No failures.');
        } catch (e) {
            if (requestId !== historyRequest) return;
            error.textContent = e.message || 'Unable to load request history.';
            error.hidden = false;
            console.error('History poll error:', e);
        }
    }
    document.getElementById('history-refresh').addEventListener('click', fetchHistory);
    document.getElementById('history-domain').addEventListener('change', fetchHistory);
    document.getElementById('history-outcome').addEventListener('change', fetchHistory);
    document.getElementById('history-tier').addEventListener('change', fetchHistory);
    document.getElementById('history-hours').addEventListener('change', fetchHistory);
    fetchHistory();
    setInterval(() => {
        if (document.getElementById('tab-history').classList.contains('active')) fetchHistory();
    }, 15000);

    function initEventStream() {
        const feed = document.getElementById('live-activity-feed');
        const statusLabel = document.getElementById('sse-status-label');
        if (!feed) return;

        let evtSource = null;
        try {
            evtSource = new EventSource('/api/events');

            evtSource.onopen = () => {
                if (statusLabel) {
                    statusLabel.textContent = 'SSE Connected';
                    statusLabel.style.color = 'var(--accent-emerald)';
                }
            };

            evtSource.onmessage = (event) => {
                try {
                    const parsed = JSON.parse(event.data);
                    if (parsed.type === 'connected') return;

                    const now = new Date();
                    const timeStr = now.toTimeString().split(' ')[0];

                    const entry = document.createElement('div');
                    entry.style.padding = '4px 0';
                    entry.style.borderBottom = '1px solid rgba(255,255,255,0.05)';

                    if (parsed.type === 'solve') {
                        const d = parsed.data;
                        const tierBadge = d.tier === 'tier1_fast_tls' 
                            ? '<span style="color: var(--accent-cyan); font-weight: 600;">[Tier 1 Fast TLS]</span>'
                            : d.tier === 'tier2_cache'
                            ? '<span style="color: var(--accent-emerald); font-weight: 600;">[Tier 2 Cache]</span>'
                            : d.tier === 'tier3_stealth_browser'
                            ? '<span style="color: var(--accent-purple); font-weight: 600;">[Tier 3 Browser]</span>'
                            : '<span style="color: var(--accent-amber); font-weight: 600;">[Tier 4 Proxy]</span>';

                        const challengeStr = d.challenge && d.challenge !== 'none' ? ` | Challenge: <b>${escapeHtml(d.challenge)}</b>` : '';
                        entry.innerHTML = `<span style="color: var(--text-muted);">${timeStr}</span> ${tierBadge} -> <code>${escapeHtml(d.url)}</code> <span style="color: var(--accent-emerald);">HTTP ${d.status}</span> (${d.duration_ms}ms, ${d.cookies_count} cookies)${challengeStr}`;
                    } else if (parsed.type === 'solve_error') {
                        const d = parsed.data;
                        entry.innerHTML = `<span style="color: var(--text-muted);">${timeStr}</span> <span style="color: var(--accent-rose); font-weight: 600;">[ERROR]</span> -> <code>${escapeHtml(d.url)}</code>: ${escapeHtml(d.error)}`;
                    }

                    if (feed.firstElementChild && feed.firstElementChild.textContent.includes('Waiting for solve')) {
                        feed.innerHTML = '';
                    }

                    feed.appendChild(entry);

                    while (feed.children.length > 50) {
                        feed.removeChild(feed.firstChild);
                    }

                    feed.scrollTop = feed.scrollHeight;
                    fetchStats();
                } catch (e) {
                    console.debug('SSE parse error:', e);
                }
            };

            evtSource.onerror = () => {
                if (statusLabel) {
                    statusLabel.textContent = 'SSE Reconnecting...';
                    statusLabel.style.color = 'var(--accent-amber)';
                }
            };
        } catch (err) {
            console.warn('SSE init notice:', err);
        }
    }

    initEventStream();

    // Live URL tester
    document.querySelectorAll('.test-preset').forEach(btn => {
        btn.addEventListener('click', () => {
            const url = btn.getAttribute('data-url');
            const method = btn.getAttribute('data-method') || 'GET';
            if (url) {
                document.getElementById('test-url').value = url;
                document.getElementById('test-method').value = method;
                const postContainer = document.getElementById('post-data-container');
                if (postContainer) {
                    if (method === 'POST') postContainer.classList.remove('hidden');
                    else postContainer.classList.add('hidden');
                }
            }
        });
    });

    const testerForm = document.getElementById('tester-form');
    const testResults = document.getElementById('test-results');
    const testSpinner = document.getElementById('test-spinner');
    const testBtnText = document.getElementById('test-btn-text');

    testerForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const url = document.getElementById('test-url').value;
        const method = document.getElementById('test-method').value;
        const postData = (method === 'POST' && document.getElementById('test-postdata')) ? document.getElementById('test-postdata').value : null;
        const forceBrowser = document.getElementById('test-force-browser').checked;
        const useCache = document.getElementById('test-use-cache').checked;
        const screenshot = document.getElementById('test-screenshot') ? document.getElementById('test-screenshot').checked : false;

        testSpinner.classList.remove('hidden');
        testBtnText.textContent = 'Solving Challenge...';
        testResults.classList.add('hidden');

        try {
            const startTime = performance.now();
            const res = await fetch('/api/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, method, postData, forceBrowser, useCache, screenshot })
            });

            const elapsed = Math.round(performance.now() - startTime);
            const data = await res.json();

            testSpinner.classList.add('hidden');
            testBtnText.textContent = 'Run Solver Test ⚡';

            if (!res.ok) {
                alert(`Error: ${data.detail || 'Failed to solve URL'}`);
                return;
            }

            testResults.classList.remove('hidden');
            document.getElementById('test-status-badge').textContent = `${data.http_status} OK (${elapsed}ms)`;

            const tbody = document.getElementById('res-cookies-body');
            tbody.innerHTML = '';
            document.getElementById('res-cookie-count').textContent = data.cookies ? data.cookies.length : 0;

            if (data.cookies && data.cookies.length > 0) {
                data.cookies.forEach(c => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td><code>${escapeHtml(c.name)}</code></td>
                        <td><code style="word-break: break-all;">${escapeHtml(c.value)}</code></td>
                        <td>${escapeHtml(c.domain || '-')}</td>
                        <td>${escapeHtml(c.path || '/')}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color: var(--text-muted);">No cookies captured</td></tr>';
            }

            document.getElementById('res-headers-code').textContent = JSON.stringify(data.headers, null, 2);
            document.getElementById('res-html-code').textContent = data.html_snippet || 'No HTML content returned';

            const screenshotTabBtn = document.getElementById('tab-btn-screenshot');
            const screenshotImg = document.getElementById('res-screenshot-img');
            if (data.screenshot) {
                screenshotTabBtn.classList.remove('hidden');
                screenshotImg.src = `data:image/jpeg;base64,${data.screenshot}`;
            } else {
                screenshotTabBtn.classList.add('hidden');
            }

            fetchStats();

        } catch (err) {
            testSpinner.classList.add('hidden');
            testBtnText.textContent = 'Run Solver Test ⚡';
            alert(`Error running test: ${err.message}`);
        }
    });

    // Result sub-tabs
    const resTabs = document.querySelectorAll('.res-tab');
    resTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.getAttribute('data-res-tab');
            resTabs.forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.res-pane').forEach(p => p.classList.remove('active'));
            
            tab.classList.add('active');
            const targetPane = document.getElementById(`res-content-${target}`);
            if (targetPane) targetPane.classList.add('active');
        });
    });

    async function fetchCookies() {
        const container = document.getElementById('cookie-domains-container');
        container.innerHTML = '<div style="color: var(--text-muted);">Loading cookie cache...</div>';
        
        try {
            const res = await fetch('/api/cookies');
            const data = await res.json();
            const domains = data.domains || {};

            if (Object.keys(domains).length === 0) {
                container.innerHTML = '<div style="color: var(--text-muted); padding: 16px 0;">No domain cookies currently cached. Run a request to populate.</div>';
                return;
            }

            container.innerHTML = '';
            for (const [domain, cookieList] of Object.entries(domains)) {
                const card = document.createElement('div');
                card.className = 'panel';
                card.style.marginBottom = '16px';
                
                let cookiesHtml = cookieList.map(c => `
                    <tr>
                        <td><code>${escapeHtml(c.name)}</code></td>
                        <td><code style="word-break: break-all;">${escapeHtml(c.value)}</code></td>
                        <td>${escapeHtml(c.age_seconds)}s ago</td>
                    </tr>
                `).join('');

                card.innerHTML = `
                    <h4 style="margin-bottom: 12px; color: var(--accent-cyan); word-break: break-all;">🌐 ${escapeHtml(domain)}</h4>
                    <div class="table-responsive">
                        <table class="data-table">
                            <thead>
                                <tr><th>Name</th><th>Value</th><th>Age</th></tr>
                            </thead>
                            <tbody>${cookiesHtml}</tbody>
                        </table>
                    </div>
                `;
                container.appendChild(card);
            }
        } catch (e) {
            container.innerHTML = '<div style="color: var(--accent-rose);">Error loading cookie cache</div>';
        }
    }

    document.getElementById('btn-clear-cookies').addEventListener('click', async () => {
        if (confirm('Are you sure you want to clear all cached domain clearance cookies?')) {
            await fetch('/api/cookies/clear', { method: 'POST' });
            fetchCookies();
            fetchStats();
        }
    });
});
