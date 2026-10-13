if (!String.prototype.startsWith) {
    String.prototype.startsWith = function (searchString, position) {
        return this.substr(position || 0, searchString.length) === searchString;
    };
}

if (!String.prototype.endsWith) {
    String.prototype.endsWith = function (searchString, position) {
        var subjectString = this.toString();
        if (typeof position !== 'number' || !isFinite(position) || Math.floor(position) !== position || position > subjectString.length) {
            position = subjectString.length;
        }
        position -= searchString.length;
        var lastIndex = subjectString.lastIndexOf(searchString, position);
        return lastIndex !== -1 && lastIndex === position;
    };
}

// https://stackoverflow.com/a/1060034/1090657
$(function () {
    var hidden = 'hidden';

    // Standards:
    if (hidden in document)
        document.addEventListener('visibilitychange', onchange);
    else if ((hidden = 'mozHidden') in document)
        document.addEventListener('mozvisibilitychange', onchange);
    else if ((hidden = 'webkitHidden') in document)
        document.addEventListener('webkitvisibilitychange', onchange);
    else if ((hidden = 'msHidden') in document)
        document.addEventListener('msvisibilitychange', onchange);
    // IE 9 and lower:
    else if ('onfocusin' in document)
        document.onfocusin = document.onfocusout = onchange;
    // All others:
    else
        window.onpageshow = window.onpagehide
            = window.onfocus = window.onblur = onchange;

    function onchange(evt) {
        var v = 'window-visible',
            h = 'window-hidden',
            evtMap = {
                focus: v, focusin: v, pageshow: v, blur: h, focusout: h, pagehide: h
            },
            state;

        evt = evt || window.event;
        if (evt.type in evtMap)
            state = evtMap[evt.type];
        else
            state = this[hidden] ? h : v;

        // Keep page-specific classes such as problem-import-preview. Replacing
        // className here previously disabled every style scoped to those classes.
        document.body.className = document.body.className
            .replace(/(^|\s)window-(?:visible|hidden)(?=\s|$)/g, ' ')
            .replace(/\s+/g, ' ')
            .replace(/^\s+|\s+$/g, '');
        document.body.className += (document.body.className ? ' ' : '') + state;

        if ('$' in window)
            $(window).trigger('dmoj:' + state);
    }

    // set the initial state (but only if browser supports the Page Visibility API)
    if (document[hidden] !== undefined)
        onchange({type: document[hidden] ? 'blur' : 'focus'});
});

function register_toggle(link) {
    link.click(function () {
        var toggled = link.next('.toggled');
        if (toggled.is(':visible')) {
            toggled.hide(400);
            link.removeClass('open');
            link.addClass('closed');
        } else {
            toggled.show(400);
            link.addClass('open');
            link.removeClass('closed');
        }
    });
}

$(function register_all_toggles() {
    $('.toggle').each(function () {
        register_toggle($(this));
    });
});

function featureTest(property, value, noPrefixes) {
    var prop = property + ':',
        el = document.createElement('test'),
        mStyle = el.style;

    if (!noPrefixes) {
        mStyle.cssText = prop + ['-webkit-', '-moz-', '-ms-', '-o-', ''].join(value + ';' + prop) + value + ';';
    } else {
        mStyle.cssText = prop + value;
    }
    return !!mStyle[property];
}

window.fix_div = function (div, height) {
    var div_offset = div.offset().top - $('html').offset().top;
    var is_moving;
    var moving = function () {
        div.css('position', 'absolute').css('top', div_offset);
        is_moving = true;
    };
    var fix = function () {
        div.css('position', 'fixed').css('top', height);
        is_moving = false;
    };
    ($(window).scrollTop() - div_offset > -height) ? fix() : moving();
    $(window).scroll(function () {
        if (($(window).scrollTop() - div_offset > -height) == is_moving)
            is_moving ? fix() : moving();
    });
};

$(function () {
    if (typeof window.orientation !== 'undefined') {
        $(window).resize(function () {
            var width = Math.max(document.documentElement.clientWidth, window.innerWidth || 0);
            $('#viewport').attr('content', width > 480 ? 'initial-scale=1' : 'width=480');
        });
    }

    var $nav_list = $('#nav-list');
    $('#navicon').click(function (event) {
        event.stopPropagation();
        $nav_list.toggleClass('show-list');
        if ($nav_list.is(':hidden'))
            $(this).blur().removeClass('hover');
        else {
            $(this).addClass('hover');
        }
    }).hover(function () {
        $(this).addClass('hover');
    }, function () {
        $(this).removeClass('hover');
    });

    $nav_list.find('li a .nav-expand').click(function (event) {
        event.preventDefault();
        $(this).parent().siblings('ul').toggleClass('show-list');
    });

    $nav_list.find('li a').each(function () {
        if (!$(this).siblings('ul').length)
            return;
        $(this).on('contextmenu', function (event) {
            event.preventDefault();
        }).on('taphold', function () {
            $(this).siblings('ul').css('display', 'block');
        });
    });

    $nav_list.click(function (event) {
        event.stopPropagation();
    });

    $('html').click(function () {
        $nav_list.removeClass('show-list');
    });

    $.ajaxSetup({
        beforeSend: function (xhr, settings) {
            if (!(/^(GET|HEAD|OPTIONS|TRACE)$/.test(settings.type)) && !this.crossDomain)
                xhr.setRequestHeader('X-CSRFToken', $.cookie('csrftoken'));
        }
    });
});

if (!Date.now) {
    Date.now = function () {
        return new Date().getTime();
    };
}

function count_down(label, redirect_url) {
    var initial = parseInt(label.attr('data-secs'));
    var start = Date.now();

    function format(num) {
        var s = "0" + num;
        return s.substr(s.length - 2);
    }

    var timer = setInterval(function () {
        var time = Math.round(initial - (Date.now() - start) / 1000);
        if (time <= 0) {
            clearInterval(timer);
            setTimeout(function() {
                if (redirect_url)
                    window.location.href = redirect_url;
                else
                    window.location.reload();
            }, 2000);
        }
        var d = Math.floor(time / 86400);
        var h = Math.floor(time % 86400 / 3600);
        var m = Math.floor(time % 3600 / 60);
        var s = time % 60;
        if (d > 0)
            label.text(npgettext('time format with day', '%d day %h:%m:%s', '%d days %h:%m:%s', d)
                .replace('%d', d).replace('%h', format(h)).replace('%m', format(m)).replace('%s', format(s)));
        else
            label.text(pgettext('time format without day', '%h:%m:%s')
                .replace('%h', format(h)).replace('%m', format(m)).replace('%s', format(s)));
    }, 1000);
}

function set_date_locale(language_code) {
    if (typeof Intl !== 'undefined' && !!Intl.RelativeTimeFormat && !!Math.trunc) {
        var rtf = new Intl.RelativeTimeFormat(language_code);
        window.format_ms = function (amount) {
            amount = Math.trunc(amount / 1000); // seconds
            if (Math.abs(amount) < 120) return rtf.format(amount, 'second');
            amount = Math.trunc(amount / 60);   // minutes
            if (Math.abs(amount) < 180) return rtf.format(amount, 'minute');
            amount = Math.trunc(amount / 60);   // hours
            if (Math.abs(amount) < 48) return rtf.format(amount, 'hour');
            amount = Math.trunc(amount / 24);   // days
            if (Math.abs(amount) < 100) return rtf.format(amount, 'day');
            return '';          // beyond 100 days, use absolute time
        };
    } else {
        window.format_ms = function () {
            return '';
        };
    }
}

function register_time(elems) {
    elems.each(function () {
        var outdated = false;
        var $this = $(this);
        var time = Date.parse($this.attr('data-iso'));
        var rel_format = $this.attr('data-format');
        var abs = $this.text();

        function update() {
            if ($('body').hasClass('window-hidden'))
                return outdated = true;
            outdated = false;
            var msg = window.format_ms(time - Date.now());
            if (!msg) {
                $this.text(abs);
                return;
            }
            $this.text(rel_format.replace('{time}', msg));
            setTimeout(update, 10000);
        }

        $(window).on('dmoj:window-visible', function () {
            if (outdated)
                update();
        });

        update();
    });
}

$(function () {
    register_time($('.time-with-rel'));

    $('form').submit(function (evt) {
        // Prevent multiple submissions of forms, see #565, #1776.
        // Skip for target="_blank" forms: the response opens in a new tab and the
        // current page is not navigated away, so leaving buttons disabled here
        // would strand the user with an unusable button until they refresh.
        if ($(this).attr('target') === '_blank') return;
        // Only disable the submit button(s) of the form being submitted. The
        // original global selector also disabled unrelated forms' submit buttons
        // (e.g. clicking a card's "Virtual" button greyed out the filter panel's
        // "Search" button on the contest list page).
        $(this).find("button[type=submit], input[type=submit]").prop('disabled', true);
    });
});

window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
        $("button[type=submit], input[type=submit]").prop('disabled', false);
    }
});

window.notification_template = {
    icon: '/logo.png'
};
window.notification_timeout = 5000;

window.notify = function (type, title, data, timeout) {
    if (localStorage[type + '_notification'] != 'true') return;
    var template = window[type + '_notification_template'] || window.notification_template;
    var data = (typeof data !== 'undefined' ? $.extend({}, template, data) : template);
    var object = new Notification(title, data);
    if (typeof timeout === 'undefined')
        timeout = window.notification_timeout;
    if (timeout)
        setTimeout(function () {
            object.close();
        }, timeout);
    return object;
};

window.register_notify = function (type, options) {
    if (typeof options === 'undefined')
        options = {};

    function status_change() {
        if ('change' in options)
            options.change(localStorage[key] == 'true');
    }

    var key = type + '_notification';
    if ('Notification' in window) {
        if (!(key in localStorage) || Notification.permission !== 'granted')
            localStorage[key] = 'false';

        if ('$checkbox' in options) {
            options.$checkbox.change(function () {
                var status = $(this).is(':checked');
                if (status) {
                    if (Notification.permission === 'granted') {
                        localStorage[key] = 'true';
                        notify(type, 'Notification enabled!');
                        status_change();
                    } else
                        Notification.requestPermission(function (permission) {
                            if (permission === 'granted') {
                                localStorage[key] = 'true';
                                notify(type, 'Notification enabled!');
                            } else localStorage[key] = 'false';
                            status_change();
                        });
                } else {
                    localStorage[key] = 'false';
                    status_change();
                }
            }).prop('checked', localStorage[key] == 'true');
        }

        $(window).on('storage', function (e) {
            e = e.originalEvent;
            if (e.key === key) {
                if ('$checkbox' in options)
                    options.$checkbox.prop('checked', e.newValue == 'true');
                status_change();
            }
        });
    } else {
        if ('$checkbox' in options) options.$checkbox.hide();
        localStorage[key] = 'false';
    }
    status_change();
};


$(function () {
    // Close dismissable boxes
    $("a.close").click(function () {
        var $closer = $(this);
        $closer.parent().fadeOut(200);
    });
});

window.dkuojInitSelect = function (select) {
    if (!select || select.dataset.ojSelectReady || select.multiple || select.hidden ||
        select.classList.contains('select2-hidden-accessible')) return;

    var wrap = document.createElement('div');
    wrap.className = 'oj-single-select multi-select';
    wrap.dataset.selectId = select.id || '';
    if (select.style.maxWidth) wrap.style.maxWidth = select.style.maxWidth;

    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'multi-select-trigger oj-single-select-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');

    var label = document.createElement('span');
    label.className = 'multi-select-text';
    var caret = document.createElement('span');
    caret.className = 'multi-select-caret';
    caret.textContent = '\u25BE';
    trigger.appendChild(label);
    trigger.appendChild(caret);

    var menu = document.createElement('div');
    menu.className = 'multi-select-menu';
    menu.setAttribute('role', 'listbox');
    menu.id = 'oj-select-menu-' + (select.id || Math.random().toString(36).slice(2));
    menu.hidden = true;
    trigger.setAttribute('aria-controls', menu.id);

    select.parentNode.insertBefore(wrap, select);
    select.classList.add('oj-single-select-native');
    select.setAttribute('tabindex', '-1');
    select.setAttribute('aria-hidden', 'true');
    wrap.appendChild(trigger);
    wrap.appendChild(menu);
    wrap.appendChild(select);

    function refresh() {
        var option = select.options[select.selectedIndex];
        label.textContent = option ? option.text : '';
        label.title = label.textContent;
        trigger.disabled = select.disabled;
    }

    function buildOptions() {
        menu.innerHTML = '';
        Array.prototype.forEach.call(select.options, function (option) {
            var item = document.createElement('button');
            item.type = 'button';
            item.className = 'multi-select-option oj-single-select-option';
            item.setAttribute('role', 'option');
            item.setAttribute('aria-selected', option.selected ? 'true' : 'false');
            item.textContent = option.text;
            item.disabled = option.disabled;
            item.addEventListener('click', function (event) {
                event.stopPropagation();
                select.value = option.value;
                select.dispatchEvent(new Event('change', {bubbles: true}));
                refresh();
                close();
            });
            menu.appendChild(item);
        });
    }

    function close() {
        wrap.classList.remove('open');
        menu.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
    }

    function open() {
        document.querySelectorAll('.multi-select.open').forEach(function (el) {
            if (el !== wrap) {
                el.classList.remove('open');
                var m = el.querySelector('.multi-select-menu');
                if (m) m.hidden = true;
            }
        });
        wrap.classList.add('open');
        menu.hidden = false;
        trigger.setAttribute('aria-expanded', 'true');
    }

    trigger.addEventListener('click', function (event) {
        event.stopPropagation();
        if (menu.hidden) open();
        else close();
    });
    trigger.addEventListener('keydown', function (event) {
        var options = Array.prototype.slice.call(menu.querySelectorAll('.oj-single-select-option:not([disabled])'));
        var currentIndex = options.findIndex(function (item) {
            return item.getAttribute('aria-selected') === 'true';
        });
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (menu.hidden) open();
            var nextIndex = event.key === 'ArrowDown' ? currentIndex + 1 : currentIndex - 1;
            nextIndex = Math.max(0, Math.min(options.length - 1, nextIndex));
            if (options[nextIndex]) options[nextIndex].focus();
        } else if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            if (menu.hidden) open();
            else close();
        }
    });
    document.addEventListener('click', function (event) {
        if (!wrap.contains(event.target)) close();
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') close();
    });
    select.addEventListener('change', function () {
        buildOptions();
        refresh();
    });

    if (window.MutationObserver) {
        new MutationObserver(function () {
            buildOptions();
            refresh();
        }).observe(select, {childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'selected']});
    }

    wrap.dkuojBuildOptions = buildOptions;
    wrap.dkuojRefresh = refresh;
    refresh();
    buildOptions();
    select.dataset.ojSelectReady = '1';
};

window.dkuojRefreshSelects = function () {
    document.querySelectorAll('.oj-single-select').forEach(function (wrap) {
        var select = wrap.querySelector('select.oj-single-select-native');
        if (select && select.dataset.ojSelectReady && wrap.dkuojBuildOptions) {
            wrap.dkuojBuildOptions();
            wrap.dkuojRefresh();
        }
    });
};

(function () {
    function initAll() {
        document.querySelectorAll('select[data-oj-select]').forEach(window.dkuojInitSelect);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        initAll();
    }

    if (window.MutationObserver) {
        var observer = new MutationObserver(initAll);
        observer.observe(document.documentElement, {childList: true, subtree: true});
    }
})();

// Bind a debounced input callback that does not fire while an IME is composing.
// Without this, the first latin letter (e.g. "q" of "question") is treated as a
// completed query: problem list live-submits and assignment list replaceState
// both abort composition, so the rest of the word is lost.
window.dkuojBindLiveSearch = function (input, callback, delayMs) {
    var timer = null;
    var composing = false;
    delayMs = delayMs || 400;

    function cancel() {
        if (timer) {
            clearTimeout(timer);
            timer = null;
        }
    }

    function schedule() {
        if (composing) return;
        cancel();
        timer = setTimeout(function () {
            timer = null;
            callback();
        }, delayMs);
    }

    if (!input) {
        return {cancel: cancel, schedule: schedule};
    }

    input.addEventListener('compositionstart', function () {
        composing = true;
        cancel();
    });
    input.addEventListener('compositionend', function () {
        composing = false;
        schedule();
    });
    input.addEventListener('input', function (event) {
        if (composing || event.isComposing) return;
        schedule();
    });

    return {cancel: cancel, schedule: schedule};
};

// Enter commits the current input as one keyword (IME-safe). The box is cleared
// so the next word can be typed. Empty Enter does nothing.
window.dkuojBindKeywordEnter = function (input, onCommit) {
    if (!input) return;
    input.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter') return;
        if (event.isComposing || event.keyCode === 229) return;
        event.preventDefault();
        var term = (input.value || '').trim();
        if (!term) return;
        input.value = '';
        onCommit(term);
    });
};

// ── Reusable auto-dismissing toast ──────────────────────────────────────
// window.showToast('Grade saved', 'success') / ('...', 'error') / ('...', 'info')
// Shows a top-right toast with a ✓ / ✗ / i icon that fades out after ~3s.
window.showToast = function (message, type) {
    type = type || 'info';
    var container = document.getElementById('dku-toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'dku-toast-container';
        container.className = 'dku-toast-container';
        document.body.appendChild(container);
    }
    var toast = document.createElement('div');
    toast.className = 'dku-toast dku-toast-' + type;
    var icon = document.createElement('span');
    icon.className = 'dku-toast-icon';
    icon.textContent = type === 'success' ? '✓' : (type === 'error' ? '✗' : 'i');
    var msg = document.createElement('span');
    msg.className = 'dku-toast-msg';
    msg.textContent = message;
    toast.appendChild(icon);
    toast.appendChild(msg);
    container.appendChild(toast);
    setTimeout(function () {
        toast.classList.add('dku-toast-leave');
        setTimeout(function () { toast.remove(); }, 200);
    }, 3000);
};
