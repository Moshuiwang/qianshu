(() => {
    const binding = "__qianshuReadonlyBlocked";
    const auditKey = "__qianshuReadonlyUnload";
    const missedKey = "__qianshuReadonlyEarlyUnload";
    let departing = false;
    let closing = false;

    // Chromium 可能丢弃旧文档卸载时的 Runtime binding 事件。同步写入并清除
    // 当前标签页的 sessionStorage，由 DOMStorage 事件把拒绝记录带出旧文档。
    const report = (method, url) => {
        if (departing && window === window.top) {
            try {
                if (window.__qianshuReadonlyAuditReady === true) {
                    const address = new URL(url);
                    sessionStorage.setItem(auditKey, JSON.stringify([method, address.origin + address.pathname]));
                    sessionStorage.removeItem(auditKey);
                    return;
                }
                // 监听尚未就绪时保留失败标记，供同源的下一份文档检查。
                sessionStorage.setItem(missedKey, "1");
            } catch {
                void window[binding](null, null);
            }
            if (window.__qianshuReadonlyAuditReady !== true) void window[binding](null, null);
        }
        void window[binding](method, url);
    };

    try {
        // 不透明源的文档没有会话存储，普通拒绝仍走绑定。
        try {
            if (sessionStorage.getItem(missedKey)) void window[binding](null, null);
        } catch {}

        const stripSpeculationRules = node => {
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            if (node.matches('script[type="speculationrules" i]')) node.remove();
            for (const script of node.querySelectorAll('script[type="speculationrules" i]')) script.remove();
        };
        new MutationObserver(records => {
            try {
                for (const record of records) {
                    if (record.type === "attributes") stripSpeculationRules(record.target);
                    else for (const node of record.addedNodes) stripSpeculationRules(node);
                }
            } catch {
                void report(null, null);
            }
        }).observe(document, {childList: true, subtree: true, attributes: true, attributeFilter: ["type"]});
        for (const script of document.querySelectorAll('script[type="speculationrules" i]')) script.remove();

        const leaving = event => {
            if (closing) {
                event.stopImmediatePropagation();
                return;
            }
            departing = event.type !== "visibilitychange" || document.visibilityState === "hidden";
        };
        for (const event of ["beforeunload", "unload", "pagehide", "visibilitychange"]) {
            window.addEventListener(event, leaving, true);
        }
        const nativeFetch = window.fetch;
        const NativeRequest = window.Request;

        window.fetch = async function (input, init) {
            const request = new NativeRequest(input, init);
            if (request.method !== "GET" || request.keepalive) {
                void report(request.method, request.url);
                throw new TypeError("只读演练已拒绝 fetch");
            }
            return nativeFetch.call(this, request);
        };

        Navigator.prototype.sendBeacon = function (url) {
            void report("POST", new URL(url, document.baseURI).href);
            return false;
        };

        if (typeof window.fetchLater === "function") {
            window.fetchLater = function (input, init) {
                let request;
                try {
                    request = new NativeRequest(input, init);
                } catch (error) {
                    void report("OTHER", "about:invalid");
                    throw error;
                }
                void report(request.method, request.url);
                throw new TypeError("只读演练已拒绝 fetchLater");
            };
        }

        const xhr = XMLHttpRequest.prototype;
        const nativeOpen = xhr.open;
        const nativeSend = xhr.send;
        const pending = new WeakMap();

        xhr.open = function (method, url, ...args) {
            const verb = String(method).toUpperCase();
            const href = new URL(url, document.baseURI).href;
            nativeOpen.call(this, verb, href, ...args);
            pending.set(this, {verb, href});
        };

        xhr.send = function (...args) {
            const request = pending.get(this);
            if (request && request.verb !== "GET") {
                void report(request.verb, request.href);
                throw new DOMException("只读演练已拒绝 XHR", "SecurityError");
            }
            return nativeSend.apply(this, args);
        };

        Object.defineProperty(window, "__qianshuReadonlyClose", {
            value: () => {
                closing = true;
                return true;
            },
        });
    } catch {
        void report(null, null);
    }
})();
