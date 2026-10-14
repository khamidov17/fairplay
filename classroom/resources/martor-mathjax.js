jQuery(function ($) {
    $(document).on('martor:preview', function (e, $content) {
        function update_math() {
            if (!(window.MathJax && typeof MathJax.typesetPromise === 'function')) return;
            MathJax.typesetPromise([$content[0]]).then(function () {
                $content.find('.tex-image').hide();
                $content.find('.tex-text').show();
            });
        }

        var $jax = $content.find('.require-mathjax-support');
        if ($jax.length) {
            // Load MathJax once per page, even when several editors preview at the same time:
            // a second copy of the library throws while registering its components.
            if (!window.dkuojMathJaxLoading) {
                if ('MathJax' in window && typeof window.MathJax.typesetPromise === 'function') {
                    window.dkuojMathJaxLoading = $.Deferred().resolve().promise();
                } else {
                    var configUrl = $jax.attr('data-config');
                    window.dkuojMathJaxLoading = $.ajax({
                        type: 'GET', url: configUrl, dataType: 'script', cache: true
                    }).then(function () {
                        window.MathJax.startup = {typeset: false};
                        return $.ajax({
                            type: 'GET',
                            // Served next to the config from this site's static files, never a CDN.
                            url: configUrl.replace(/mathjax_config\.js(\?.*)?$/, 'libs/mathjax/es5/tex-chtml.js'),
                            dataType: 'script',
                            cache: true
                        });
                    });
                }
            }
            window.dkuojMathJaxLoading.then(update_math, function () {
                window.dkuojMathJaxLoading = null;  // let the next preview try again
            });
        }
    });
});
