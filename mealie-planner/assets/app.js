function planner() {
  return {
    /* state */
    days: [],
    dayOffset: 0,
    pastDays:   Math.min(14, Math.max(0, parseInt(localStorage.getItem('pastDays')   ?? '0',  10))),
    futureDays: Math.min(14, Math.max(0, parseInt(localStorage.getItem('futureDays') ?? '7',  10))),
    colorTheme: localStorage.getItem('colorTheme') || 'system',
    themeMenuOpen: false,
    planLoading: false,
    planError: null,
    _slots: {},          // flat map: "date:mt" → entry | null (drives reactivity)

    allRecipes: [],
    recipesLoaded: false,

    modalOpen: false,
    modalDate: null,
    modalMt: null,
    modalSearch: '',
    modalLimit: 24,

    configured: false,
    mode: 'docker',
    mealieReachable: false,
    mealieVersion: null,

    settingsOpen: false,
    settingsForm: { mealie_url: '', api_token: '' },
    settingsSaving: false,
    settingsError: null,

    cacheRefreshing: false,
    cacheCount: null,

    sparkling: {},   // "date:mt" → true
    draggedSlot: null,
    toasts: [],

    tooltipRecipe: null,
    tooltipX: 0,
    tooltipY: 0,

    undoBar: false,
    undoMessage: '',
    pendingActions: [],

    activeCell: null,  // { date, mt } last clicked cell

    recipeActions: [],
    actionMenuOpen: false,
    actionMenuRecipe: null,
    actionMenuX: 0,
    actionMenuY: 0,
    actionLoading: null,

    mobileDays: [],
    mobileLoadingMore: false,
    mobileHasMore: true,
    _mobileObserver: null,

    enabledMealTypes: JSON.parse(localStorage.getItem('enabledMealTypes') || '["dinner"]'),
    _recentRecipes: JSON.parse(localStorage.getItem('recentRecipes') || '[]'),

    /* computed */
    get filteredRecipes() {
      if (!this.modalSearch) return this.allRecipes;
      const q = this.modalSearch.toLowerCase();
      return this.allRecipes.filter(r => r.name.toLowerCase().includes(q));
    },

    get weekRangeLabel() {
      if (!this.days.length) return '';
      const parse = s => { const [y,m,d] = s.split('-').map(Number); return new Date(y,m-1,d); };
      const a = parse(this.days[0].date), b = parse(this.days[this.days.length - 1].date);
      if (a.getMonth() === b.getMonth()) {
        return `${a.getDate()}–${b.getDate()} ${a.toLocaleDateString(undefined, {month:'long'})} ${a.getFullYear()}`;
      }
      return `${a.toLocaleDateString(undefined,{day:'numeric',month:'short'})} – ${b.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})}`;
    },

    get gridItems() {
      const items = [{ t: 'corner' }];
      for (const d of this.days) items.push({ t:'dh', date:d.date, wd:d.wd, dn:d.dn, today:d.isToday });
      for (const mt of this.enabledMealTypes) {
        items.push({ t:'ml', mt });
        for (const d of this.days) items.push({ t:'cell', date:d.date, mt, today:d.isToday });
      }
      return items;
    },

    get recentRecipeObjects() {
      return this._recentRecipes.map(id => this.allRecipes.find(r => r.id === id)).filter(Boolean);
    },

    get visibleRecipes() {
      return this.filteredRecipes.slice(0, this.modalLimit);
    },

    get hasMoreRecipes() {
      return this.modalLimit < this.filteredRecipes.length;
    },

    /* slots */
    slotKey(date, mt) { return date + ':' + mt; },
    getSlot(date, mt) { return date ? this._slots[this.slotKey(date, mt)] || null : null; },
    setSlot(date, mt, val) {
      this._slots = { ...this._slots, [this.slotKey(date, mt)]: val };
    },
    isSparkle(date, mt) { return !!(date && this.sparkling[this.slotKey(date, mt)]); },

    /* cell class */
    cellClass(item) {
      if (item.t === 'corner')  return 'g-corner';
      if (item.t === 'dh')      return item.today ? 'g-day-hdr g-day-hdr--today' : 'g-day-hdr';
      if (item.t === 'ml')      return 'g-meal-lbl g-meal-lbl--' + item.mt;
      if (item.t === 'cell')    return item.today ? 'g-cell g-cell--today' : 'g-cell';
    },

    /* theme */
    applyTheme() {
      const el = document.documentElement;
      if (this.colorTheme === 'system') el.removeAttribute('data-theme');
      else el.setAttribute('data-theme', this.colorTheme);
    },
    setTheme(mode) {
      this.colorTheme = mode;
      this.themeMenuOpen = false;
      localStorage.setItem('colorTheme', mode);
      this.applyTheme();
    },

    /* init */
    async init() {
      this.applyTheme();
      this.buildDays();
      try {
        const status = await this._fetch('/api/status');
        this.configured      = status.configured;
        this.mode            = status.mode;
        this.mealieReachable = status.mealie_reachable;
        this.mealieVersion   = status.version;
        if (!this.configured) { this.settingsOpen = true; return; }
        const cfg = await this._fetch('/api/config');
        this.settingsForm.mealie_url = cfg.mealie_url;
        await Promise.all([this.loadMealPlan(), this.loadRecipes(), this.loadRecipeActions()]);
        await this.initMobileScroll();
      } catch (e) {
        this.toast('Failed to reach backend. Is the server running?');
      }
    },

    buildDays() {
      const now = new Date();
      const todayStr = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
      const anchor = new Date(now);
      anchor.setDate(now.getDate() + this.dayOffset);
      this.days = [];
      for (let i = -this.pastDays; i <= this.futureDays; i++) {
        const d = new Date(anchor); d.setDate(anchor.getDate() + i);
        const date = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        this.days.push({
          date,
          isToday: date === todayStr,
          label: date === todayStr ? 'Today' : d.toLocaleDateString(undefined, {weekday:'long'}),
          wd: d.toLocaleDateString(undefined, {weekday:'short'}),
          dn: d.getDate(),
        });
      }
    },

    async rebuildAndReload() {
      localStorage.setItem('pastDays',   String(this.pastDays));
      localStorage.setItem('futureDays', String(this.futureDays));
      this.buildDays();
      await this.loadMealPlan();
    },

    /* loading */
    async loadMealPlan() {
      if (!this.days.length) return;
      this.planLoading = true;
      this.planError   = null;
      try {
        const start = this.days[0].date, end = this.days.at(-1).date;
        const entries = await this._fetch(`/api/mealplan?start_date=${start}&end_date=${end}`);
        // clear existing slots for this window
        const next = { ...this._slots };
        for (const d of this.days) for (const mt of ['breakfast','lunch','dinner','side']) delete next[this.slotKey(d.date, mt)];
        for (const e of entries) next[this.slotKey(e.date, e.meal_type)] = this._prefixImg(e);
        this._slots = next;
      } catch (e) {
        this.planError = e.message || 'Could not load meal plan.';
      } finally {
        this.planLoading = false;
      }
    },

    async loadRecipes() {
      try {
        this.allRecipes   = (await this._fetch('/api/recipes')).map(r => this._prefixImg(r));
        this.cacheCount   = this.allRecipes.length;
        this.recipesLoaded = true;
      } catch (e) {
        this.toast('Could not load recipe cache — try refreshing it in settings.');
        this.recipesLoaded = true;
      }
    },

    /* nav */
    async shiftPage(delta) {
      this.dayOffset += delta;
      this.buildDays();
      await this.loadMealPlan();
      this.scrollToToday();
    },
    async goToToday() {
      this.dayOffset = 0;
      this.buildDays();
      await this.loadMealPlan();
      this.scrollToToday();
    },
    scrollToToday() {
      if (this.dayOffset !== 0) return;
      this.$nextTick(() => {
        document.querySelector('.mobile-week-day--today')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    },

    /* modal */
    openModal(date, mt) {
      this.modalDate = date; this.modalMt = mt; this.modalSearch = ''; this.modalLimit = 24; this.modalOpen = true;
      if (!this.recipesLoaded) this.loadRecipes();
      this.$nextTick(() => this.$refs.searchInput?.focus());
    },

    onModalScroll(event) {
      const el = event.currentTarget;
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 300) {
        this.modalLimit += 24;
      }
    },

    async selectRecipe(recipe) {
      const date = this.modalDate, mt = this.modalMt;
      const prev = this.getSlot(date, mt);
      this.setSlot(date, mt, { recipe_id: recipe.id, recipe_name: recipe.name, image_url: recipe.image_url, recipe_slug: recipe.slug, id: null, _optimistic: true });
      this.modalOpen = false;
      try {
        const entry = await this._post('/api/mealplan', { date, meal_type: mt, recipe_id: recipe.id });
        this.setSlot(date, mt, this._prefixImg(entry));
        this.pushRecentRecipe(recipe.id);
      } catch (e) {
        this.setSlot(date, mt, prev);
        this.toast('Failed to save — ' + (e.message || 'please try again.'));
      }
    },

    async removeRecipe(date, mt, entryId) {
      const prev = this.getSlot(date, mt);
      if (!prev) return;

      // Optimistically clear the slot
      this.setSlot(date, mt, null);

      // Fire DELETE immediately — no more 7-second delay
      try {
        if (entryId) await this._delete(`/api/mealplan/${entryId}`);
      } catch (e) {
        this.setSlot(date, mt, prev);
        this.toast('Failed to remove — ' + (e.message || 'please try again.'));
        return;
      }

      // Undo window: re-create the entry via POST
      const id = Date.now() + Math.random();
      this.pendingActions.push({ id, date, mt, prev, message: 'Removed ' + prev.recipe_name });
      this.undoBar = true;
      this.undoMessage = 'Removed ' + prev.recipe_name;

      setTimeout(() => {
        const idx = this.pendingActions.findIndex(a => a.id === id);
        if (idx === -1) return;
        this.pendingActions.splice(idx, 1);
        if (!this.pendingActions.length) this.undoBar = false;
      }, 7000);
    },

    async sparkle(date, mt) {
      const key = this.slotKey(date, mt);
      this.sparkling = { ...this.sparkling, [key]: true };
      try {
        const recipe = await this._fetch(`/api/sparkle?date=${date}&meal_type=${mt}`);
        const entry  = await this._post('/api/mealplan', { date, meal_type: mt, recipe_id: recipe.id });
        this.setSlot(date, mt, this._prefixImg(entry));
      } catch (e) {
        this.toast('Sparkle failed — ' + (e.message || 'no recipes cached?'));
      } finally {
        const next = { ...this.sparkling }; delete next[key]; this.sparkling = next;
      }
    },

    /* tooltip */
    showTooltip(date, mt, event) {
      const slot = this.getSlot(date, mt);
      if (!slot) return;
      const recipe = this.allRecipes.find(r => r.id === slot.recipe_id);
      if (!recipe) return;
      this.tooltipRecipe = recipe;
      const chip = event.currentTarget.closest('.chip, .mobile-recipe-row');
      if (!chip) return;
      const r = chip.getBoundingClientRect();
      this.tooltipX = r.left + r.width / 2;
      this.tooltipY = r.top;
    },
    hideTooltip() { this.tooltipRecipe = null; },

    /* keyboard */
    setActiveCell(date, mt) { if (date && mt) this.activeCell = { date, mt }; },
    sparkleActive() { if (this.activeCell) this.sparkle(this.activeCell.date, this.activeCell.mt); },
    onKeydown(event) {
      if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA') return;
      if (event.key === 'ArrowLeft') { event.preventDefault(); if (!this.modalOpen) this.shiftPage(-1); }
      else if (event.key === 'ArrowRight') { event.preventDefault(); if (!this.modalOpen) this.shiftPage(1); }
      else if (event.key === 'r' || event.key === 'R') { if (!this.modalOpen) this.sparkleActive(); }
      else if (event.key === 'Escape') { this.modalOpen = false; this.themeMenuOpen = false; }
      else if (event.key === 'Tab' && this.modalOpen) {
        const modal = document.querySelector('.modal');
        if (!modal) return;
        const focusable = [...modal.querySelectorAll('button:not([disabled]), input, a, [tabindex]:not([tabindex="-1"])')];
        if (focusable.length < 2) return;
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey) { if (document.activeElement === first) { event.preventDefault(); last.focus(); } }
        else { if (document.activeElement === last) { event.preventDefault(); first.focus(); } }
      }
    },

    /* undo */
    undoLastAction() {
      const action = this.pendingActions.pop();
      if (!action) return;
      if (!this.pendingActions.length) this.undoBar = false;

      const { date, mt, prev } = action;
      this.setSlot(date, mt, prev);

      this._post('/api/mealplan', { date, meal_type: mt, recipe_id: prev.recipe_id })
        .then(entry => this.setSlot(date, mt, this._prefixImg(entry)))
        .catch(() => this.toast('Could not undo.'));
    },

    /* recent */
    pushRecentRecipe(recipeId) {
      this._recentRecipes = [recipeId, ...this._recentRecipes.filter(id => id !== recipeId)].slice(0, 12);
      localStorage.setItem('recentRecipes', JSON.stringify(this._recentRecipes));
    },

    /* drag */
    onDragStart(date, mt, event) {
      const entry = this.getSlot(date, mt);
      if (!entry) return;
      this.draggedSlot = { date, mt, entry };
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', '');
      event.target.closest('.chip')?.classList.add('dragging');
    },

    onDragEnd() {
      this.draggedSlot = null;
      document.querySelectorAll('.g-cell.drag-over, .mobile-slot.drag-over').forEach(el => el.classList.remove('drag-over'));
      document.querySelectorAll('.chip.dragging').forEach(el => el.classList.remove('dragging'));
    },

    onDragOver(event) {
      document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
      event.currentTarget.closest('.g-cell, .mobile-slot')?.classList.add('drag-over');
    },

    onDragLeave(event) {
      const target = event.currentTarget.closest('.g-cell, .mobile-slot');
      if (target && (!event.relatedTarget || !target.contains(event.relatedTarget))) {
        target.classList.remove('drag-over');
      }
    },

    async onDrop(targetDate, targetMt) {
      document.querySelectorAll('.g-cell.drag-over, .mobile-slot.drag-over').forEach(el => el.classList.remove('drag-over'));

      if (!this.draggedSlot || !targetDate || !targetMt) return;

      const { date: srcDate, mt: srcMt, entry: srcEntry } = this.draggedSlot;
      this.draggedSlot = null;

      if (srcDate === targetDate && srcMt === targetMt) return;

      const tgtEntry = this.getSlot(targetDate, targetMt);

      // optimistic swap
      this.setSlot(targetDate, targetMt, srcEntry);
      this.setSlot(srcDate, srcMt, tgtEntry);

      try {
        const deletions = [];
        if (srcEntry?.id) deletions.push(this._delete(`/api/mealplan/${srcEntry.id}`));
        if (tgtEntry?.id) deletions.push(this._delete(`/api/mealplan/${tgtEntry.id}`));
        await Promise.all(deletions);

        const creations = [];
        creations.push(this._post('/api/mealplan', { date: targetDate, meal_type: targetMt, recipe_id: srcEntry.recipe_id }));
        if (tgtEntry) creations.push(this._post('/api/mealplan', { date: srcDate, meal_type: srcMt, recipe_id: tgtEntry.recipe_id }));
        const results = await Promise.all(creations);

        this.setSlot(targetDate, targetMt, this._prefixImg(results[0]));
        if (results[1]) this.setSlot(srcDate, srcMt, this._prefixImg(results[1]));
      } catch (e) {
        // rollback
        this.setSlot(srcDate, srcMt, srcEntry);
        this.setSlot(targetDate, targetMt, tgtEntry);
        this.toast('Failed to move recipe — ' + (e.message || 'please try again.'));
      }
    },

    /* settings */
    async saveSettings() {
      this.settingsSaving = true; this.settingsError = null;
      try {
        await this._post('/api/config', this.settingsForm);
        const status = await this._fetch('/api/status');
        this.configured      = status.configured;
        this.mealieReachable = status.mealie_reachable;
        this.mealieVersion   = status.version;
        this.settingsForm.api_token = '';
        this.settingsOpen = false;
        await Promise.all([this.loadMealPlan(), this.loadRecipes()]);
      } catch (e) {
        this.settingsError = e.message || 'Save failed.';
      } finally {
        this.settingsSaving = false;
      }
    },

    toggleMealType(type) {
      const order = ['breakfast','lunch','dinner','side'];
      this.enabledMealTypes = this.enabledMealTypes.includes(type)
        ? this.enabledMealTypes.filter(t => t !== type)
        : order.filter(t => [...this.enabledMealTypes, type].includes(t));
      localStorage.setItem('enabledMealTypes', JSON.stringify(this.enabledMealTypes));
    },

    async refreshCache() {
      this.cacheRefreshing = true;
      try {
        const r = await this._post('/api/cache/refresh', {});
        this.cacheCount = r.count;
        await this.loadRecipes();
        this.toast(`Cache refreshed — ${r.count} recipes.`, 'success');
      } catch (e) {
        this.toast('Cache refresh failed.');
      } finally {
        this.cacheRefreshing = false;
      }
    },

    /* toasts */
    toast(msg, type = 'error') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      setTimeout(() => this.removeToast(id), 5000);
    },
    removeToast(id) { this.toasts = this.toasts.filter(t => t.id !== id); },

    /* format */
    formatDate(dateStr) {
      if (!dateStr) return '';
      const [y,m,d] = dateStr.split('-').map(Number);
      return new Date(y,m-1,d).toLocaleDateString(undefined, {weekday:'short',month:'short',day:'numeric'});
    },
    getMealieLink(slug) {
      return slug ? api('/api/recipe-link/' + slug) : '#';
    },

    /* recipe actions */
    async loadRecipeActions() {
      try {
        this.recipeActions = await this._fetch('/api/recipe-actions');
      } catch {}
    },

    openActionMenu(slot, event) {
      if (!slot) return;
      this.actionMenuRecipe = { slug: slot.recipe_slug, name: slot.recipe_name };
      const rect = event.currentTarget.getBoundingClientRect();
      let x = rect.left, y = rect.bottom + 6;
      if (x + 200 > window.innerWidth) x = window.innerWidth - 208;
      if (y + 160 > window.innerHeight) y = rect.top - 6;
      this.actionMenuX = x;
      this.actionMenuY = y;
      this.actionMenuOpen = true;
    },

    async triggerRecipeAction(actionId, recipeSlug) {
      this.actionLoading = actionId;
      try {
        const result = await this._post(`/api/recipe-actions/${actionId}/trigger`, { recipe_slug: recipeSlug });
        if (result.type === 'link' && result.url) {
          window.open(result.url, '_blank', 'noopener');
        } else {
          this.toast('Action sent.', 'success');
        }
        this.actionMenuOpen = false;
      } catch (e) {
        this.toast('Action failed — ' + (e.message || 'unknown error'));
      } finally {
        this.actionLoading = null;
      }
    },

    /* mobile infinite scroll */
    _dateStr(d) {
      return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    },

    _buildMobileBatch(startDate, count) {
      const today = this._dateStr(new Date());
      return Array.from({ length: count }, (_, i) => {
        const d = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate() + i);
        const date = this._dateStr(d);
        return {
          date,
          isToday: date === today,
          label: date === today ? 'Today' : d.toLocaleDateString(undefined, { weekday: 'long' }),
          wd: d.toLocaleDateString(undefined, { weekday: 'short' }),
          dn: d.getDate(),
        };
      });
    },

    async initMobileScroll() {
      if (this._mobileObserver) { this._mobileObserver.disconnect(); this._mobileObserver = null; }
      this.mobileLoadingMore = false;
      this.mobileHasMore = true;
      const now = new Date();
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - this.pastDays);
      const batch = this._buildMobileBatch(start, this.pastDays + 1 + Math.min(6, this.futureDays));
      this.mobileDays = batch;
      try {
        const entries = await this._fetch(`/api/mealplan?start_date=${batch[0].date}&end_date=${batch.at(-1).date}`);
        const next = { ...this._slots };
        for (const e of entries) next[this.slotKey(e.date, e.meal_type)] = this._prefixImg(e);
        this._slots = next;
      } catch {}
      await this.$nextTick();
      document.querySelector('.mobile-week-day--today')?.scrollIntoView({ behavior: 'instant', block: 'start' });
      const sentinel = document.getElementById('mobile-scroll-sentinel');
      if (!sentinel || !('IntersectionObserver' in window)) return;
      this._mobileObserver = new IntersectionObserver(async ([entry]) => {
        if (entry.isIntersecting) await this.loadMoreMobileDays();
      }, { rootMargin: '400px' });
      this._mobileObserver.observe(sentinel);
    },

    async loadMoreMobileDays() {
      const MAX = 60;
      if (this.mobileLoadingMore || !this.mobileHasMore) return;
      if (this.mobileDays.length >= MAX) { this.mobileHasMore = false; return; }
      this.mobileLoadingMore = true;
      const [y, m, d] = this.mobileDays.at(-1).date.split('-').map(Number);
      const batch = this._buildMobileBatch(new Date(y, m-1, d+1), Math.min(7, MAX - this.mobileDays.length));
      try {
        const entries = await this._fetch(`/api/mealplan?start_date=${batch[0].date}&end_date=${batch.at(-1).date}`);
        const next = { ...this._slots };
        for (const e of entries) next[this.slotKey(e.date, e.meal_type)] = this._prefixImg(e);
        this._slots = next;
        this.mobileDays = [...this.mobileDays, ...batch];
        if (this.mobileDays.length >= MAX) this.mobileHasMore = false;
      } catch { this.toast('Could not load more days.'); }
      finally { this.mobileLoadingMore = false; }
    },

    /* prefix bare /api/ image paths with INGRESS_PATH */
    _prefixImg(obj) {
      if (!obj || !obj.image_url) return obj;
      if (obj.image_url.startsWith('/api/')) return { ...obj, image_url: api(obj.image_url) };
      return obj;
    },

    /* http */
    async _fetch(path) {
      const r = await fetch(api(path));
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${r.status}`);
      }
      return r.json();
    },
    async _post(path, body) {
      const r = await fetch(api(path), { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
      if (!r.ok) {
        const body2 = await r.json().catch(() => ({}));
        throw new Error(body2.detail || `HTTP ${r.status}`);
      }
      return r.json().catch(() => ({}));
    },
    async _delete(path) {
      const r = await fetch(api(path), { method:'DELETE' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    },
  };
}
