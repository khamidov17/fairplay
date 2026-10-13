function initializeProblemCodeAutofill() {
    const codeInput = document.getElementById('id_code');
    if (!codeInput) return;

    const url = new URL(window.location.href);
    const isOriginalAdvancedPage = url.searchParams.get('advanced') === '1' ||
        document.querySelector('input[name="_advanced_mode"][value="1"]');

    const isAddPage = window.location.pathname.endsWith('/add/');
    if (!isAddPage) return;

    async function checkProblemCode(code) {
        if (!code || code.trim() === '') {
            return null;
        }

        try {
            const response = await fetch(`/api/v2/problem/check-code/?code=${encodeURIComponent(code)}`, {
                method: 'GET',
                cache: 'no-cache',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });

            if (!response.ok) {
                return null;
            }

            const data = await response.json();
            return data.exists;
        } catch (error) {
            console.error('Checking Failed:', error);
            return null;
        }
    }

    async function generateUniqueCloneCode() {
        const response = await fetch('/api/v2/problem/check-code/?suggest=1', {
            method: 'GET',
            cache: 'no-cache',
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        });
        if (!response.ok) {
            throw new Error('suggest failed');
        }
        const data = await response.json();
        if (!data.code) {
            throw new Error('suggest failed');
        }
        return data.code;
    }

    async function autofillProblemCode(options) {
        const config = options || {};
        try {
            const uniqueCode = await generateUniqueCloneCode();
            codeInput.value = uniqueCode;
            if (!config.silent) {
                codeInput.dispatchEvent(new Event('change', {bubbles: true}));
            }
            return uniqueCode;
        } catch (error) {
            console.error('Failed to generate code:', error);
            return null;
        }
    }

    window.DKUOJAutofillProblemCode = autofillProblemCode;
    if (isOriginalAdvancedPage && codeInput.value.trim() === '') {
        autofillProblemCode();
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeProblemCodeAutofill);
} else {
    initializeProblemCodeAutofill();
}
