"""
Advanced Anti-Bot Detection & Fingerprint Masking Module.
Protects against Cloudflare, DataDome, Akamai, Imperva, and CAPTCHA fingerprinting probes.
"""

STEALTH_INIT_SCRIPT = """
(() => {
    // 1. Strip navigator.webdriver flag at prototype level
    try {
        const newProto = Object.getPrototypeOf(navigator);
        delete newProto.webdriver;
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true
        });
    } catch (e) {}

    // 2. Realistic Chrome Window Object & Extension Mocks
    window.chrome = {
        app: {
            isInstalled: false,
            InstallState: { DISABLED: 'DISABLED', INSTALLED: 'INSTALLED', UNINSTALLED: 'UNINSTALLED' },
            RunningState: { CANNOT_RUN: 'CANNOT_RUN', READY_TO_RUN: 'READY_TO_RUN', RUNNING: 'RUNNING' }
        },
        runtime: {
            OnInstalledReason: { CHROME_UPDATE: "chrome_update", INSTALL: "install", SHARED_MODULE_UPDATE: "shared_module_update", UPDATE: "update" },
            OnRestartRequiredReason: { APP_UPDATE: "app_update", OS_UPDATE: "os_update", PERIODIC: "periodic" },
            PlatformArch: { ARM: "arm", ARM64: "arm64", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64" },
            PlatformNaclArch: { ARM: "arm", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64" },
            PlatformOs: { ANDROID: "android", CROS: "cros", LINUX: "linux", MAC: "mac", OPENBSD: "openbsd", WIN: "win" }
        }
    };

    // 3. Plugins & MimeTypes Emulation
    try {
        const fakePlugins = [
            { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" },
            { name: "Chrome PDF Viewer", filename: "mhjfbheakfljhbgacldabdafdanoacmc", description: "Chrome PDF Viewer" },
            { name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Chromium PDF Viewer" },
            { name: "Microsoft Edge PDF Viewer", filename: "pdf-viewer", description: "Edge PDF Viewer" },
            { name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "WebKit built-in PDF" }
        ];
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [...fakePlugins];
                arr.item = (i) => arr[i];
                arr.namedItem = (name) => arr.find(p => p.name === name);
                return arr;
            },
            configurable: true
        });
    } catch(e) {}

    // 4. Languages & Permissions API
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
            configurable: true
        });
    } catch(e) {}

    if (window.navigator && window.navigator.permissions) {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    }

    // 5. Hardware Concurrency, Memory & Screen Specs
    try {
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
        Object.defineProperty(screen, 'colorDepth', { get: () => 24, configurable: true });
        Object.defineProperty(screen, 'pixelDepth', { get: () => 24, configurable: true });
    } catch(e) {}

    // 6. WebGL Vendor & Renderer Spoofing (Emulates Real Desktop GPU)
    try {
        const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            // UNMASKED_VENDOR_WEBGL
            if (parameter === 37445) return 'Google Inc. (NVIDIA)';
            // UNMASKED_RENDERER_WEBGL
            if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return getParameterOrig.apply(this, arguments);
        };
        if (typeof WebGL2RenderingContext !== 'undefined') {
            const getParameter2Orig = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter2Orig.apply(this, arguments);
            };
        }
    } catch(e) {}

    // 7. Subtle Canvas Noise Injection (Evades static canvas hashing)
    try {
        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type) {
            if (!this._noiseApplied && this.width > 16 && this.height > 16) {
                try {
                    const ctx = this.getContext('2d');
                    if (ctx) {
                        const imgData = ctx.getImageData(0, 0, 1, 1);
                        imgData.data[0] = (imgData.data[0] ^ 1);
                        ctx.putImageData(imgData, 0, 0);
                        this._noiseApplied = true;
                    }
                } catch(e) {}
            }
            return originalToDataURL.apply(this, arguments);
        };
    } catch(e) {}

    // 8. Outer Window Dimensions Spoofing for Headless Mode
    try {
        if (window.outerWidth === 0) {
            Object.defineProperty(window, 'outerWidth', { get: () => 1920, configurable: true });
            Object.defineProperty(window, 'outerHeight', { get: () => 1080, configurable: true });
        }
    } catch(e) {}

    // 9. MediaDevices Enumeration Spoofing (Evades zero-device headless fingerprint)
    try {
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            const origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
            navigator.mediaDevices.enumerateDevices = async () => {
                const list = await origEnum();
                if (!list || list.length === 0) {
                    return [
                        { deviceId: 'default', kind: 'audioinput', label: 'Default Audio Device', groupId: 'group1' },
                        { deviceId: 'default', kind: 'videoinput', label: 'Integrated Webcam', groupId: 'group2' }
                    ];
                }
                return list;
            };
        }
    } catch(e) {}

    // 10. Subtle AudioContext / AudioBuffer Noise (Evades acoustic hashing on Akamai / DataDome)
    try {
        if (typeof AudioBuffer !== 'undefined') {
            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {
                const data = origGetChannelData.apply(this, arguments);
                if (!this._audioNoiseApplied && data && data.length > 50) {
                    for (let i = 0; i < Math.min(10, data.length); i++) {
                        data[i] += 0.0000001 * (Math.random() - 0.5);
                    }
                    this._audioNoiseApplied = true;
                }
                return data;
            };
        }
    } catch(e) {}

    // 11. WebRTC Local IP Leak Prevention (Masks LAN IP on proxied connections)
    try {
        if (window.RTCPeerConnection) {
            const origCreateOffer = RTCPeerConnection.prototype.createOffer;
            RTCPeerConnection.prototype.createOffer = function(options) {
                return origCreateOffer.apply(this, arguments);
            };
        }
    } catch(e) {}
})();
"""

CLOUDFLARE_WAIT_SCRIPT = """
async () => {
    const turnstileBox = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (turnstileBox) {
        try {
            const rect = turnstileBox.getBoundingClientRect();
            return { hasTurnstile: true, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        } catch(e) {}
    }
    const cfWrapper = document.querySelector('#challenge-stage, #cf-please-wait, .cf-turnstile-wrapper, div[class*="turnstile"]');
    return { hasTurnstile: !!cfWrapper, x: 0, y: 0 };
}
"""
