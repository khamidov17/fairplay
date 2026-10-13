(function (global) {
  function translate(message) {
    return typeof gettext === 'function' ? gettext(message) : message;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function preserveLineBreaks(html) {
    return String(html == null ? '' : html)
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .replace(/\n/g, '<br>');
  }

  function pendingLabel(status) {
    if (status === 'QU') return translate('Queued...');
    if (status === 'P') return translate('Processing...');
    if (status === 'G') return translate('Grading...');
    return '';
  }

  function isComplete(status) {
    return status === 'D' || status === 'IE' || status === 'CE' || status === 'AB';
  }

  function formatCompletedRunParts(data) {
    var info = '';
    if (data.status === 'CE') {
      info = escapeHtml(translate('Compilation Error')) + '\n\n';
      info += data.error || escapeHtml(translate('(no error message)'));
      return { infoHtml: preserveLineBreaks(info), outputHtml: '' };
    }
    if (data.status === 'IE') {
      info = escapeHtml(translate('Internal Error')) + '\n\n';
      info += escapeHtml(translate(
        'An error occurred on the judge server. Please try again or contact an administrator.',
      ));
      return { infoHtml: preserveLineBreaks(info), outputHtml: '' };
    }
    if (data.status === 'AB') {
      info = escapeHtml(translate('Aborted')) + '\n\n';
      info += escapeHtml(translate('The submission was aborted.'));
      return { infoHtml: preserveLineBreaks(info), outputHtml: '' };
    }

    info = escapeHtml(translate('Result:') + ' ' + (data.result || 'N/A'));
    if (data.result === 'AC') {
      info += '  ' + escapeHtml(translate('(sandbox ran successfully; output may be incorrect)'));
    }
    info += '\n';
    if (data.feedback || data.extended_feedback) {
      if (data.feedback) {
        info += escapeHtml(translate('Details:') + ' ') + data.feedback + '\n';
      }
      if (data.extended_feedback) {
        info += data.extended_feedback + '\n';
      }
    }
    if (data.time !== null && data.time !== undefined) {
      info += escapeHtml(translate('Time:') + ' ' + Number(data.time).toFixed(3) + 's') + '\n';
    }
    if (data.memory !== null && data.memory !== undefined) {
      info += escapeHtml(translate('Memory:') + ' ' + Number(data.memory).toFixed(0) + ' KB') + '\n';
    }
    if (data.output_hidden) {
      info += '\n' + escapeHtml(translate('Output has been hidden'));
      return { infoHtml: preserveLineBreaks(info), outputHtml: '' };
    }
    return { infoHtml: preserveLineBreaks(info), outputHtml: data.output || '' };
  }

  function formatCompletedRun(data) {
    var parts = formatCompletedRunParts(data);
    if (!parts.outputHtml) {
      return parts.infoHtml;
    }
    return parts.infoHtml + '<br><br>' + escapeHtml(translate('Output:')) + '<br>' + parts.outputHtml;
  }

  global.formatIdeRunParts = formatCompletedRunParts;
  global.formatIdeRunResult = formatCompletedRun;
  global.formatIdeRunPending = pendingLabel;
  global.isIdeRunComplete = isComplete;
}(window));
