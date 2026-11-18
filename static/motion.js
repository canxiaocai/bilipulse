/**
 * BiliPulse // SIGNAL STUDIO — Kinetic Motion Choreography Engine
 * Zero external dependencies. Compositor-friendly 60fps transitions & reduced-motion aware.
 */

let activeThemeTransitionToken = 0;

/**
 * Checks whether the user prefers reduced motion at the OS/browser level.
 * @returns {boolean}
 */
export function prefersReducedMotion() {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

/**
 * Animates a numeric readout element with exponential ease-out rolling figures
 * and directional telemetry flash states (.is-rolling, .pulse-up, .pulse-down).
 *
 * @param {HTMLElement | null} el
 * @param {number} targetValue
 * @param {object} [options]
 * @param {number} [options.duration=680]
 * @param {(n: number) => string} [options.formatter]
 * @param {boolean} [options.flash=true]
 */
export function animateNumber(el, targetValue, options = {}) {
  if (!el) return;

  const {
    duration = 680,
    formatter = (n) => Math.round(n).toLocaleString('zh-CN'),
    flash = true,
  } = options;

  if (el._rafId) {
    cancelAnimationFrame(el._rafId);
    el._rafId = 0;
  }

  const target = Number(targetValue);
  if (!Number.isFinite(target)) {
    el.classList.remove('is-rolling');
    el.textContent = targetValue == null ? '--' : String(targetValue);
    delete el.dataset.numValue;
    return;
  }

  const rawPrev = el.dataset.numValue;
  const parsedPrev =
    rawPrev !== undefined && rawPrev !== '' ? Number(rawPrev) : Number.NaN;
  const hasPrevious = Number.isFinite(parsedPrev);
  const startValue = hasPrevious ? parsedPrev : target * 0.92;
  const valueChanged = hasPrevious && target !== parsedPrev;

  el.dataset.numValue = String(target);

  if (flash && valueChanged) {
    if (el._flashTimer) {
      clearTimeout(el._flashTimer);
      el._flashTimer = 0;
    }
    el.classList.remove('pulse-up', 'pulse-down');
    const dirClass = target > parsedPrev ? 'pulse-up' : 'pulse-down';
    el.classList.add(dirClass);
    el._flashTimer = setTimeout(() => {
      el.classList.remove('pulse-up', 'pulse-down');
      el._flashTimer = 0;
    }, 650);
  }

  if (
    prefersReducedMotion() ||
    duration <= 0 ||
    Math.abs(target - startValue) < 1e-6
  ) {
    el.classList.remove('is-rolling');
    el.textContent = formatter(target);
    return;
  }

  const totalDuration = Math.max(16, Number(duration) || 680);
  const startTime = performance.now();
  el.classList.add('is-rolling');

  const tick = (now) => {
    const elapsed = Math.max(0, now - startTime);
    const t = Math.min(1, elapsed / totalDuration);
    const eased = t >= 1 ? 1 : 1 - Math.pow(2, -10 * t);
    const current = t >= 1 ? target : startValue + (target - startValue) * eased;

    el.textContent = formatter(current);

    if (t < 1) {
      el._rafId = requestAnimationFrame(tick);
    } else {
      el._rafId = 0;
      el.classList.remove('is-rolling');
      el.textContent = formatter(target);
    }
  };

  el._rafId = requestAnimationFrame(tick);
}

/**
 * Wraps DOM state mutations inside the native View Transitions API with origin
 * coordinates (--vt-x, --vt-y) and theme transition radial-clip hook (.vt-theme),
 * falling back cleanly when unsupported or under prefers-reduced-motion.
 *
 * @param {() => void} mutateDom
 * @param {object} [options]
 * @param {HTMLElement | null} [options.originEl=null]
 * @param {'tab' | 'theme' | string} [options.type='tab']
 * @returns {Promise<void>}
 */
export function runViewTransition(mutateDom, options = {}) {
  if (typeof mutateDom !== 'function') {
    return Promise.resolve();
  }

  const { originEl = null, type = 'tab' } = options;
  const root =
    typeof document !== 'undefined' ? document.documentElement : null;

  if (
    root &&
    originEl &&
    typeof originEl.getBoundingClientRect === 'function'
  ) {
    const rect = originEl.getBoundingClientRect();
    const cx = Math.round(rect.left + rect.width / 2);
    const cy = Math.round(rect.top + rect.height / 2);
    root.style.setProperty('--vt-x', `${cx}px`);
    root.style.setProperty('--vt-y', `${cy}px`);
  }

  if (
    !root ||
    typeof document.startViewTransition !== 'function' ||
    prefersReducedMotion()
  ) {
    mutateDom();
    return Promise.resolve();
  }

  const isTheme = type === 'theme';
  const token = isTheme ? ++activeThemeTransitionToken : 0;
  if (isTheme) {
    root.classList.add('vt-theme');
  }

  let transition;
  try {
    transition = document.startViewTransition(() => {
      mutateDom();
    });
  } catch (_err) {
    if (isTheme && token === activeThemeTransitionToken) {
      root.classList.remove('vt-theme');
    }
    mutateDom();
    return Promise.resolve();
  }

  if (transition.ready && typeof transition.ready.catch === 'function') {
    transition.ready.catch(() => {});
  }
  if (
    transition.updateCallbackDone &&
    typeof transition.updateCallbackDone.catch === 'function'
  ) {
    transition.updateCallbackDone.catch(() => {});
  }

  return transition.finished
    .catch(() => {})
    .finally(() => {
      if (isTheme && token === activeThemeTransitionToken) {
        root.classList.remove('vt-theme');
      }
    });
}

/**
 * Triggers staggered entrance choreography on matching child elements inside a container,
 * capping --stagger-i to 0..16 so large tables stay responsive and cleaning up .is-entering.
 *
 * @param {ParentNode | null} container
 * @param {string} [selector='[data-stagger]']
 */
export function staggerEnter(container, selector = '[data-stagger]') {
  if (!container || typeof container.querySelectorAll !== 'function') return;

  const elements = Array.from(container.querySelectorAll(selector));
  if (elements.length === 0) return;

  const reduced = prefersReducedMotion();

  for (let i = 0; i < elements.length; i += 1) {
    const el = elements[i];
    const cappedIndex = Math.min(i, 16);
    el.style.setProperty('--stagger-i', String(cappedIndex));

    if (el._staggerTimer) {
      clearTimeout(el._staggerTimer);
      el._staggerTimer = 0;
    }
    if (el._staggerEndHandler) {
      el.removeEventListener('animationend', el._staggerEndHandler);
      el.removeEventListener('animationcancel', el._staggerEndHandler);
      el._staggerEndHandler = null;
    }
    el.classList.remove('is-entering');
  }

  if (reduced) {
    return;
  }

  // Batch a single layout read so removing/adding .is-entering reliably restarts keyframes
  if (container instanceof HTMLElement) {
    void container.offsetWidth;
  }

  for (let i = 0; i < elements.length; i += 1) {
    const el = elements[i];
    const cappedIndex = Math.min(i, 16);

    const cleanup = () => {
      if (el._staggerTimer) {
        clearTimeout(el._staggerTimer);
        el._staggerTimer = 0;
      }
      if (el._staggerEndHandler) {
        el.removeEventListener('animationend', el._staggerEndHandler);
        el.removeEventListener('animationcancel', el._staggerEndHandler);
        el._staggerEndHandler = null;
      }
      el.classList.remove('is-entering');
    };

    const onAnimFinish = (event) => {
      if (event.target !== el) return;
      cleanup();
    };

    el._staggerEndHandler = onAnimFinish;
    el.addEventListener('animationend', onAnimFinish);
    el.addEventListener('animationcancel', onAnimFinish);
    el.classList.add('is-entering');

    // If no CSS animation runs on this element, clean up promptly; also keep hard timeout safety
    requestAnimationFrame(() => {
      if (
        el.classList.contains('is-entering') &&
        typeof el.getAnimations === 'function' &&
        el.offsetParent !== null &&
        el.getAnimations().length === 0
      ) {
        cleanup();
      }
    });

    el._staggerTimer = setTimeout(cleanup, 920 + cappedIndex * 42);
  }
}

/**
 * Positions a sliding active rail/bar indicator relative to its offsetParent,
 * supporting both vertical desktop rails ('y') and horizontal rails/range bars ('x').
 *
 * @param {HTMLElement | null} indicatorEl
 * @param {HTMLElement | null} activeEl
 * @param {'auto' | 'x' | 'y'} [axis='auto']
 */
export function positionSlidingIndicator(indicatorEl, activeEl, axis = 'auto') {
  if (!indicatorEl) return;
  if (!activeEl) {
    indicatorEl.style.opacity = '0';
    return;
  }

  const parent = indicatorEl.offsetParent || indicatorEl.parentElement;
  if (!parent || typeof window === 'undefined') return;

  let resolvedAxis = axis;
  if (resolvedAxis !== 'x' && resolvedAxis !== 'y') {
    const parentStyle = window.getComputedStyle(parent);
    if (parentStyle.display.includes('flex')) {
      resolvedAxis = parentStyle.flexDirection.startsWith('column') ? 'y' : 'x';
    } else if (parentStyle.display.includes('grid')) {
      const cols = parentStyle.gridTemplateColumns
        .trim()
        .split(/\s+/)
        .filter(Boolean);
      resolvedAxis = cols.length <= 1 ? 'y' : 'x';
    } else {
      resolvedAxis = 'x';
    }
  }

  const w = activeEl.offsetWidth;
  const h = activeEl.offsetHeight;
  if (w === 0 && h === 0) {
    return;
  }

  let x = 0;
  let y = 0;
  if (activeEl.offsetParent === parent) {
    x = activeEl.offsetLeft;
    y = activeEl.offsetTop;
  } else {
    const activeRect = activeEl.getBoundingClientRect();
    const parentRect = parent.getBoundingClientRect();
    x =
      activeRect.left -
      parentRect.left +
      parent.scrollLeft -
      (parent.clientLeft || 0);
    y =
      activeRect.top -
      parentRect.top +
      parent.scrollTop -
      (parent.clientTop || 0);
  }

  let tx = Math.round(x);
  let ty = Math.round(y);

  if (resolvedAxis === 'y') {
    indicatorEl.style.width = '';
    indicatorEl.style.left = '';
    indicatorEl.style.right = '';
    const indStyle = window.getComputedStyle(indicatorEl);
    const cssW = parseFloat(indStyle.width) || 0;
    const isRightPinned =
      indStyle.left === 'auto' && parseFloat(indStyle.right) === 0;

    indicatorEl.style.top = '0px';
    indicatorEl.style.bottom = 'auto';
    indicatorEl.style.height = `${Math.round(h)}px`;

    if (cssW > 0 && cssW <= 12) {
      // Thin vertical edge rail defined by CSS (e.g. 2px-4px laser bar)
      if (isRightPinned) {
        tx = 0;
      } else {
        indicatorEl.style.left = '0px';
      }
    } else {
      // Full-cell sliding frame/box highlight
      indicatorEl.style.left = '0px';
      indicatorEl.style.width = `${Math.round(w)}px`;
    }
  } else {
    indicatorEl.style.height = '';
    indicatorEl.style.top = '';
    indicatorEl.style.bottom = '';
    const indStyle = window.getComputedStyle(indicatorEl);
    const cssH = parseFloat(indStyle.height) || 0;
    const isBottomPinned =
      indStyle.bottom !== 'auto' &&
      parseFloat(indStyle.bottom) <= 4 &&
      (indStyle.top === 'auto' || parseFloat(indStyle.top) > 12);

    indicatorEl.style.left = '0px';
    indicatorEl.style.right = 'auto';
    indicatorEl.style.width = `${Math.round(w)}px`;

    if (cssH > 0 && cssH <= 12) {
      // Thin horizontal underline/overline laser bar defined by CSS
      if (isBottomPinned) {
        ty = 0;
      } else {
        indicatorEl.style.top = '0px';
      }
    } else {
      // Full-button sliding frame/box highlight
      indicatorEl.style.top = '0px';
      indicatorEl.style.height = `${Math.round(h)}px`;
    }
  }

  indicatorEl.dataset.axis = resolvedAxis;
  indicatorEl.style.opacity = '1';
  indicatorEl.style.transform = `translate3d(${tx}px, ${ty}px, 0)`;
}
