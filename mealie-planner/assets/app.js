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
    planRefreshing: false,
    planError: null,
    _slots: {},

    allRecipes: [],
    recipesLoaded: false,

    modalOpen: false,
    modalDate: null,
    modalMt: null,
    modalSearch: '',
    modalLimit: 24,
    modalMode: 'add',        // 'add' | 'replace'
    modalReplaceEntry: null, // entry being replaced in replace mode

    initialized: false,
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
    _scrollY: 0,

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

    /* slots — values are entry[] */
    slotKey(date, mt) { return date + ':' + mt; },
    getSlot(date, mt) { return date ? (this._slots[this.slotKey(date, mt)] || []) : []; },
    hasSlot(date, mt) { return this.getSlot(date, mt).length > 0; },
    setSlot(date, mt, arr) {
      this._slots = { ...this._slots, [this.slotKey(date, mt)]: arr };
    },
    isSparkle(date, mt) { return !!(date && this.sparkling[this.slotKey(date, mt)]); },

    /* cell class */
    cellClass(item) {
      if (item.t === 'corner')  return 'g-corner';
      if (item.t === 'dh')      return item.today ? 'g-day-hdr g-day-hdr--today' : 'g-day-hdr';
      if (item.t === 'ml')      return 'g-meal-lbl g-meal-lbl--' + item.mt;
      if (item.t === 'cell')    return (item.today ? 'g-cell g-cell--today' : 'g-cell') + ' g-cell--' + item.mt;
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
      } finally {
        this.initialized = true;
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
      const start = this.days[0].date, end = this.days.at(-1).date;
      this.planError = null;

      const cached = this._loadPlanCache(start, end);
      if (cached) {
        this._applyPlanEntries(cached);
        this.planRefreshing = true;
      } else {
        this.planLoading = true;
      }

      try {
        const entries = await this._fetch(`/api/mealplan?start_date=${start}&end_date=${end}`);
        this._applyPlanEntries(entries);
        this._savePlanCache(start, end, entries);
      } catch (e) {
        this.planError = e.message || 'Could not load meal plan.';
      } finally {
        this.planLoading = false;
        this.planRefreshing = false;
      }
    },

    _applyPlanEntries(entries) {
      const next = { ...this._slots };
      for (const d of this.days) for (const mt of ['breakfast','lunch','dinner','side']) next[this.slotKey(d.date, mt)] = [];
      for (const e of entries) {
        const key = this.slotKey(e.date, e.meal_type);
        next[key] = [...(next[key] || []), this._prefixImg(e)];
      }
      this._slots = next;
    },

    _savePlanCache(start, end, entries) {
      try {
        localStorage.setItem('mp_plan', JSON.stringify({ start, end, entries, ts: Date.now() }));
      } catch {}
    },

    _loadPlanCache(start, end) {
      try {
        const raw = localStorage.getItem('mp_plan');
        if (!raw) return null;
        const c = JSON.parse(raw);
        if (c.start !== start || c.end !== end) return null;
        return c.entries;
      } catch { return null; }
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

    _onSettingsToggle(open) {
      if (window.matchMedia('(max-width: 719px)').matches) {
        open ? this._lockBodyScroll() : this._unlockBodyScroll();
      }
    },

    /* iOS-safe body scroll lock — overflow:hidden alone doesn't work on Safari */
    _lockBodyScroll() {
      this._scrollY = window.scrollY;
      document.body.style.overflow = 'hidden';
      document.body.style.position = 'fixed';
      document.body.style.top = `-${this._scrollY}px`;
      document.body.style.width = '100%';
    },
    _unlockBodyScroll() {
      document.body.style.overflow = '';
      document.body.style.position = '';
      document.body.style.top = '';
      document.body.style.width = '';
      window.scrollTo(0, this._scrollY);
    },

    /* modal */
    openModal(date, mt) {
      this.modalDate = date; this.modalMt = mt; this.modalSearch = ''; this.modalLimit = 24;
      this.modalMode = 'add'; this.modalReplaceEntry = null;
      this.modalOpen = true;
      if (!this.recipesLoaded) this.loadRecipes();
      this.$nextTick(() => this.$refs.searchInput?.focus());
    },

    openModalReplace(date, mt, entry) {
      this.modalDate = date; this.modalMt = mt; this.modalSearch = ''; this.modalLimit = 24;
      this.modalMode = 'replace'; this.modalReplaceEntry = entry;
      this.modalOpen = true;
      if (!this.recipesLoaded) this.loadRecipes();
      this.$nextTick(() => this.$refs.searchInput?.focus());
    },

    async modalSparkle() {
      const date = this.modalDate, mt = this.modalMt;
      this.modalOpen = false;
      await this.sparkle(date, mt);
    },

    onModalScroll(event) {
      const el = event.currentTarget;
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 300) {
        this.modalLimit += 24;
      }
    },

    async selectRecipe(recipe) {
      const date = this.modalDate, mt = this.modalMt;
      this.modalOpen = false;

      if (this.modalMode === 'replace' && this.modalReplaceEntry) {
        const oldEntry = this.modalReplaceEntry;
        const prev = [...this.getSlot(date, mt)];
        const optimistic = { recipe_id: recipe.id, recipe_name: recipe.name, image_url: recipe.image_url, recipe_slug: recipe.slug, id: null, _optimistic: true };
        this.setSlot(date, mt, prev.map(e => e.id === oldEntry.id ? optimistic : e));
        try {
          if (oldEntry.id) await this._delete(`/api/mealplan/${oldEntry.id}`);
          const entry = await this._post('/api/mealplan', { date, meal_type: mt, recipe_id: recipe.id });
          this.setSlot(date, mt, this.getSlot(date, mt).map(e => e._optimistic && e.recipe_id === recipe.id ? this._prefixImg(entry) : e));
          this.pushRecentRecipe(recipe.id);
        } catch (e) {
          this.setSlot(date, mt, prev);
          this.toast('Failed to save — ' + (e.message || 'please try again.'));
        }
        return;
      }

      // add mode — append to slot
      const optimistic = { recipe_id: recipe.id, recipe_name: recipe.name, image_url: recipe.image_url, recipe_slug: recipe.slug, id: null, _optimistic: true };
      this.setSlot(date, mt, [...this.getSlot(date, mt), optimistic]);
      try {
        const entry = await this._post('/api/mealplan', { date, meal_type: mt, recipe_id: recipe.id });
        const arr = this.getSlot(date, mt);
        const idx = arr.findIndex(e => e._optimistic && e.recipe_id === recipe.id);
        if (idx !== -1) {
          const updated = [...arr];
          updated[idx] = this._prefixImg(entry);
          this.setSlot(date, mt, updated);
        }
        this.pushRecentRecipe(recipe.id);
      } catch (e) {
        this.setSlot(date, mt, this.getSlot(date, mt).filter(e => !(e._optimistic && e.recipe_id === recipe.id)));
        this.toast('Failed to save — ' + (e.message || 'please try again.'));
      }
    },

    async removeRecipe(date, mt, entryId) {
      const prev = this.getSlot(date, mt);
      const entry = prev.find(e => e.id === entryId);
      if (!entry) return;

      this.setSlot(date, mt, prev.filter(e => e.id !== entryId));

      try {
        if (entryId) await this._delete(`/api/mealplan/${entryId}`);
      } catch (e) {
        this.setSlot(date, mt, prev);
        this.toast('Failed to remove — ' + (e.message || 'please try again.'));
        return;
      }

      const id = Date.now() + Math.random();
      this.pendingActions.push({ id, date, mt, prev: entry, message: 'Removed ' + entry.recipe_name });
      this.undoBar = true;
      this.undoMessage = 'Removed ' + entry.recipe_name;

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
        this.setSlot(date, mt, [...this.getSlot(date, mt), this._prefixImg(entry)]);
        this.toast(`✦ ${entry.recipe_name}`, 'info');
      } catch (e) {
        this.toast('Sparkle failed — ' + (e.message || 'no recipes cached?'));
      } finally {
        const next = { ...this.sparkling }; delete next[key]; this.sparkling = next;
      }
    },

    /* tooltip */
    showTooltip(entry, event) {
      if (!entry) return;
      const recipe = this.allRecipes.find(r => r.id === entry.recipe_id);
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
      else if (event.key === 'Escape') { this.modalOpen = false; this.themeMenuOpen = false; this.settingsOpen = false; this.actionMenuOpen = false; }
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

    /* undo — re-creates the single removed entry */
    undoLastAction() {
      const action = this.pendingActions.pop();
      if (!action) return;
      if (!this.pendingActions.length) this.undoBar = false;

      const { date, mt, prev: entry } = action;
      const optimistic = { ...entry, _optimistic: true };
      this.setSlot(date, mt, [...this.getSlot(date, mt), optimistic]);

      this._post('/api/mealplan', { date, meal_type: mt, recipe_id: entry.recipe_id })
        .then(newEntry => {
          const arr = this.getSlot(date, mt);
          const idx = arr.findIndex(e => e._optimistic && e.recipe_id === entry.recipe_id);
          if (idx !== -1) {
            const updated = [...arr];
            updated[idx] = this._prefixImg(newEntry);
            this.setSlot(date, mt, updated);
          }
        })
        .catch(() => {
          this.setSlot(date, mt, this.getSlot(date, mt).filter(e => !(e._optimistic && e.recipe_id === entry.recipe_id)));
          this.toast('Could not undo.');
        });
    },

    /* recent */
    pushRecentRecipe(recipeId) {
      this._recentRecipes = [recipeId, ...this._recentRecipes.filter(id => id !== recipeId)].slice(0, 12);
      localStorage.setItem('recentRecipes', JSON.stringify(this._recentRecipes));
    },

    /* drag — moves a single entry to target slot (no swap) */
    onDragStart(date, mt, entry, event) {
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

      // Optimistic: remove from src, append to target
      this.setSlot(srcDate, srcMt, this.getSlot(srcDate, srcMt).filter(e => e.id !== srcEntry.id));
      this.setSlot(targetDate, targetMt, [...this.getSlot(targetDate, targetMt), srcEntry]);

      try {
        if (srcEntry?.id) await this._delete(`/api/mealplan/${srcEntry.id}`);
        const created = await this._post('/api/mealplan', { date: targetDate, meal_type: targetMt, recipe_id: srcEntry.recipe_id });
        this.setSlot(targetDate, targetMt, this.getSlot(targetDate, targetMt).map(
          e => e.id === srcEntry.id ? this._prefixImg(created) : e
        ));
      } catch (e) {
        // rollback
        this.setSlot(srcDate, srcMt, [...this.getSlot(srcDate, srcMt), srcEntry]);
        this.setSlot(targetDate, targetMt, this.getSlot(targetDate, targetMt).filter(e => e.id !== srcEntry.id));
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

    openActionMenu(entry, event) {
      if (!entry) return;
      this.actionMenuRecipe = { slug: entry.recipe_slug, name: entry.recipe_name };
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
      // loadMealPlan already fetched this date range — just set mobileDays, no separate fetch needed
      this.mobileDays = batch;
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
        for (const d of batch) for (const mt of ['breakfast','lunch','dinner','side']) next[this.slotKey(d.date, mt)] = [];
        for (const e of entries) {
          const key = this.slotKey(e.date, e.meal_type);
          next[key] = [...(next[key] || []), this._prefixImg(e)];
        }
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
