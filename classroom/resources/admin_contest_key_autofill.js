function initializeContestKeyAutofill() {
    const keyInput = document.getElementById('id_key');
    if (!keyInput) return;

    const url = new URL(window.location.href);
    const isOriginalAdvancedPage = url.searchParams.get('advanced') === '1' ||
        document.querySelector('input[name="_advanced_mode"][value="1"]');

    const isAddPage = window.location.pathname.endsWith('/add/');
    if (!isAddPage) return;

    async function checkContestKey(key) {
        if (!key || key.trim() === '') return null;

        try {
            const response = await fetch(`/api/v2/contest/check-key/?key=${encodeURIComponent(key)}`, {
                method: 'GET',
                cache: 'no-cache',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });

            if (!response.ok) return null;

            const data = await response.json();
            return data.exists;
        } catch (e) {
            console.error(e);
            return null;
        }
    }

    async function generateUniqueContestKey() {
        const response = await fetch('/api/v2/contest/check-key/?suggest=1', {
            method: 'GET',
            cache: 'no-cache',
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        });
        if (!response.ok) throw new Error('suggest failed');
        const data = await response.json();
        if (!data.key) throw new Error('suggest failed');
        return data.key;
    }

    async function autofillContestKey(options) {
        const config = options || {};
        try {
            const key = await generateUniqueContestKey();
            keyInput.value = key;
            if (!config.silent) {
                keyInput.dispatchEvent(new Event('change', {bubbles: true}));
            }
            return key;
        } catch (e) {
            console.error('Failed to generate contest key:', e);
            return null;
        }
    }

    window.DKUOJAutofillContestKey = autofillContestKey;
    if (isOriginalAdvancedPage && keyInput.value.trim() === '') {
        autofillContestKey();
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeContestKeyAutofill);
} else {
    initializeContestKeyAutofill();
}
