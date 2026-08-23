document.addEventListener('DOMContentLoaded', () => {
    // Tab Navigation
    const navItems = document.querySelectorAll('.nav-item');
    const tabPages = document.querySelectorAll('.tab-page');
    const tabTitle = document.getElementById('current-tab-title');

    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const tabName = item.getAttribute('data-tab');
            
            navItems.forEach(n => n.classList.remove('active'));
            tabPages.forEach(p => p.classList.remove('active'));
            
            item.classList.add('active');
            document.getElementById(`tab-${tabName}`).classList.add('active');
            
            tabTitle.textContent = item.querySelector('span').textContent;
            
            if (tabName === 'cookies') {
                fetchCookies();
            }
        });
    });

    // Toggle POST body field
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

    // Stats Fetcher & Poller
    async function fetchStats() {
        try {
            const res = await fetch('/api/stats');
            if (!res.ok) return;
            const data = await res.json();

            document.getElementById('val-total-reqs').textContent = data.total_requests || 0;
            document.getElementById('val-fast-rate').textContent = (data.fast_hit_rate_pct || 0) + '%';
            document.getElementById('val-fast-hits').textContent = (data.tier1_fast_tls_hits || 0) + (data.tier2_cache_hits || 0);
            document.getElementById('val-fast-ms').innerHTML = `${data.avg_fast_ms || 0}<span class="unit">ms</span>`;
            document.getElementById('val-browser-ms').textContent = data.avg_browser_ms || 0;
            document.getElementById('val-ram').innerHTML = `${data.ram_usage_mb || 0}<span class="unit">MB</span>`;
            document.getElementById('val-cpu').textContent = (data.cpu_usage_pct || 0) + '%';

            // 4-Tier Pipeline counters
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
            if (data.version && document.getElementById('val-version-tag')) {
                document.getElementById('val-version-tag').textContent = data.version;
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

    // Real-Time Server-Sent Events (SSE) Listener
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

                        const challengeStr = d.challenge && d.challenge !== 'none' ? ` | Challenge: <b>${d.challenge}</b>` : '';
                        entry.innerHTML = `<span style="color: var(--text-muted);">${timeStr}</span> ${tierBadge} -> <code>${d.url}</code> <span style="color: var(--accent-emerald);">HTTP ${d.status}</span> (${d.duration_ms}ms, ${d.cookies_count} cookies)${challengeStr}`;
                    } else if (parsed.type === 'solve_error') {
                        const d = parsed.data;
                        entry.innerHTML = `<span style="color: var(--text-muted);">${timeStr}</span> <span style="color: var(--accent-rose); font-weight: 600;">[ERROR]</span> -> <code>${d.url}</code>: ${d.error}`;
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

    // Live URL Tester Form
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

            // Display Results
            testResults.classList.remove('hidden');
            document.getElementById('test-status-badge').textContent = `${data.http_status} OK (${elapsed}ms)`;

            // Render cookies
            const tbody = document.getElementById('res-cookies-body');
            tbody.innerHTML = '';
            document.getElementById('res-cookie-count').textContent = data.cookies ? data.cookies.length : 0;

            if (data.cookies && data.cookies.length > 0) {
                data.cookies.forEach(c => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td><code>${c.name}</code></td>
                        <td><code style="word-break: break-all;">${c.value}</code></td>
                        <td>${c.domain || '-'}</td>
                        <td>${c.path || '/'}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color: var(--text-muted);">No cookies captured</td></tr>';
            }

            // Render Headers & HTML
            document.getElementById('res-headers-code').textContent = JSON.stringify(data.headers, null, 2);
            document.getElementById('res-html-code').textContent = data.html_snippet || 'No HTML content returned';

            // Screenshot tab handling
            const screenshotTabBtn = document.getElementById('tab-btn-screenshot');
            const screenshotImg = document.getElementById('res-screenshot-img');
            if (data.screenshot) {
                screenshotTabBtn.classList.remove('hidden');
                screenshotImg.src = `data:image/jpeg;base64,${data.screenshot}`;
            } else {
                screenshotTabBtn.classList.add('hidden');
            }

            // Refresh overview stats
            fetchStats();

        } catch (err) {
            testSpinner.classList.add('hidden');
            testBtnText.textContent = 'Run Solver Test ⚡';
            alert(`Error running test: ${err.message}`);
        }
    });

    // Results Sub-tabs
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

    // Cookie Cache Inspector
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
                        <td><code>${c.name}</code></td>
                        <td><code style="word-break: break-all;">${c.value}</code></td>
                        <td>${c.age_seconds}s ago</td>
                    </tr>
                `).join('');

                card.innerHTML = `
                    <h4 style="margin-bottom: 12px; color: var(--accent-cyan);">🌐 ${domain}</h4>
                    <table class="data-table">
                        <thead>
                            <tr><th>Name</th><th>Value</th><th>Age</th></tr>
                        </thead>
                        <tbody>${cookiesHtml}</tbody>
                    </table>
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
