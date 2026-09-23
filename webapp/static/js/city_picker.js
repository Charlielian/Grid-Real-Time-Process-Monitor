/*
 * 地市输入下拉选择器。
 *
 * 组件用隐藏的同名 city 字段提交多城市条件，避免改变后端现有的
 * request.args.getlist("city") 解析方式。可见区域只负责搜索、枚举展示、
 * 已选标签和取消选择；真实提交值始终以 data-city-selected 中的 hidden input 为准。
 */
(() => {
  const pickers = document.querySelectorAll('[data-city-picker]');
  if (!pickers.length) return;

  pickers.forEach((picker) => {
    const search = picker.querySelector('[data-city-search]');
    const panel = picker.querySelector('[data-city-panel]');
    const options = [...picker.querySelectorAll('[data-city-option]')];
    const suggestions = picker.querySelector('[data-city-suggestions]');
    const empty = picker.querySelector('[data-city-empty]');
    const count = picker.querySelector('[data-city-count]');
    const selectedBox = picker.querySelector('[data-city-selected]');
    const selectAll = picker.querySelector('[data-city-select-all]');
    const clear = picker.querySelector('[data-city-clear]');
    if (!search || !panel || !suggestions || !empty || !count || !selectedBox || !selectAll || !clear) return;

    const normalize = (value) => value.trim().toLocaleLowerCase('zh-CN');
    const selectedNames = () => [...selectedBox.querySelectorAll('[data-city-value]')].map((input) => input.value);
    const isSelected = (city) => selectedNames().includes(city);
    const syncState = () => {
      const names = selectedNames();
      count.textContent = names.length ? `已选 ${names.length} 个` : '未选择（全部）';
      options.forEach((option) => {
        const selected = isSelected(option.dataset.city);
        option.classList.toggle('is-selected', selected);
        option.setAttribute('aria-selected', String(selected));
      });
    };
    const addCity = (city) => {
      if (isSelected(city)) return;
      const tag = document.createElement('span');
      tag.className = 'city-tag';
      tag.dataset.cityTag = city;
      tag.textContent = city;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'city-tag-remove';
      remove.dataset.cityRemove = city;
      remove.setAttribute('aria-label', `移除${city}`);
      remove.textContent = '×';
      tag.append(remove);
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'city';
      hidden.value = city;
      hidden.dataset.cityValue = city;
      selectedBox.append(tag, hidden);
      syncState();
    };
    const removeCity = (city) => {
      selectedBox.querySelector(`[data-city-tag="${CSS.escape(city)}"]`)?.remove();
      selectedBox.querySelector(`[data-city-value="${CSS.escape(city)}"]`)?.remove();
      syncState();
    };
    const closePanel = () => {
      panel.hidden = true;
      suggestions.hidden = true;
      search.setAttribute('aria-expanded', 'false');
    };
    const openPanel = () => {
      panel.hidden = false;
      search.setAttribute('aria-expanded', 'true');
      render();
    };
    const choose = (option) => {
      if (isSelected(option.dataset.city)) removeCity(option.dataset.city);
      else addCity(option.dataset.city);
      search.value = '';
      render();
      search.focus();
    };
    const renderSuggestions = (matches, query) => {
      suggestions.replaceChildren();
      if (!query || !matches.length) {
        suggestions.hidden = true;
        return;
      }
      matches.forEach((option) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'city-suggestion';
        item.setAttribute('role', 'option');
        item.textContent = option.dataset.city;
        item.addEventListener('click', () => choose(option));
        suggestions.append(item);
      });
      suggestions.hidden = false;
    };
    const render = () => {
      const query = normalize(search.value);
      const matches = options.filter((option) => normalize(option.dataset.city).includes(query));
      options.forEach((option) => { option.hidden = Boolean(query) && !matches.includes(option); });
      empty.hidden = !query || matches.length > 0;
      renderSuggestions(matches, query);
      syncState();
    };

    search.addEventListener('focus', openPanel);
    search.addEventListener('input', render);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { closePanel(); return; }
      if (event.key === 'Enter' && !suggestions.hidden) {
        const first = suggestions.querySelector('.city-suggestion');
        if (first) { event.preventDefault(); first.click(); }
      }
    });
    options.forEach((option) => option.addEventListener('click', () => choose(option)));
    selectedBox.addEventListener('click', (event) => {
      const remove = event.target.closest('[data-city-remove]');
      if (remove) removeCity(remove.dataset.cityRemove);
    });
    selectAll.addEventListener('click', () => {
      openPanel();
      options.forEach((option) => addCity(option.dataset.city));
      search.value = '';
      render();
    });
    clear.addEventListener('click', () => {
      selectedBox.replaceChildren();
      search.value = '';
      openPanel();
      syncState();
      search.focus();
    });
    document.addEventListener('click', (event) => { if (!picker.contains(event.target)) closePanel(); });
    syncState();
  });
})();
