// Documentation remains usable without JavaScript.
if (navigator.clipboard && window.isSecureContext) {
  document.querySelectorAll('pre').forEach((pre) => {
    const code = pre.querySelector('code');
    if (!code) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'copy-button';
    button.textContent = 'Copy';
    button.setAttribute('aria-label', 'Copy code block');
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        button.textContent = 'Copied';
      } catch {
        button.textContent = 'Select to copy';
      }
      window.setTimeout(() => {
        button.textContent = 'Copy';
      }, 2000);
    });
    pre.prepend(button);
  });
}
