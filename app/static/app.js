// @ts-check
'use strict';

// Switching profiles is a regular, CSRF-protected form submission.
/** @type {NodeListOf<HTMLSelectElement>} */
const profileSwitches = document.querySelectorAll('[data-profile-switch]');
profileSwitches.forEach(/** @param {HTMLSelectElement} select @returns {void} */ select => {
	/** @type {HTMLFormElement | null} */
	const form = select.form;
	if (!form) return;

	select.addEventListener('change', /** @returns {void} */() => form.requestSubmit());
	/** @type {HTMLElement | null} */
	const button = form.querySelector('.profile-submit');
	if (button) button.hidden = true;
});

/** @type {HTMLFormElement | null} */
const periodForm = document.querySelector('[data-period-form]');
if (periodForm) {
	/** @type {HTMLInputElement | null} */
	const input = periodForm.querySelector('[data-period-date]');
	if (input) {
		/** @returns {void} */
		const setPeriod = () => {
			/** @type {HTMLInputElement | null} */
			const selected = periodForm.querySelector('[name="period"]:checked');
			if (!selected) return;
			/** @type {string} */
			const period = selected.value;
			/** @type {HTMLInputElement | null} */
			const endInput = periodForm.querySelector('[data-period-end-input]');
			/** @type {HTMLElement | null} */
			const endLabel = periodForm.querySelector('[data-period-end]');
			/** @type {HTMLElement | null} */
			const startLabel = periodForm.querySelector('[data-period-start-label]');
			if (endInput) { endInput.disabled = period !== 'range'; endInput.required = period === 'range'; }
			if (endLabel) endLabel.hidden = period !== 'range';
			if (startLabel) startLabel.textContent = period === 'range' ? 'С даты' : period === 'week' ? 'По дату включительно' : 'Период';
			/** @type {string} */
			const old = input.value;
			input.type = period === 'year' ? 'number' : period === 'month' ? 'month' : 'date';
			input.removeAttribute('min');
			input.removeAttribute('max');
			if (period === 'year') {
				input.min = '1'; input.max = '9999'; input.step = '1';
				input.value = old.slice(0, 4);
			}
			else if (period === 'month') {
				input.value = old.length >= 7 ? old.slice(0, 7) : `${old}-01`;
			}
			else {
				input.value = old.length === 10 ? old : old.length === 7 ? `${old}-01` : `${old}-01-01`;
			}
		};
		/** @type {NodeListOf<HTMLInputElement>} */
		const radios = periodForm.querySelectorAll('[name="period"]');
		radios.forEach(/** @param {HTMLInputElement} radio @returns {void} */ radio => {
			radio.addEventListener('change', setPeriod);
		});
		setPeriod();
	}
}

/** @type {HTMLDialogElement | null} */
const confirmation = document.querySelector('#confirmation');
/** @type {HTMLFormElement | null} */
let pendingForm = null;
/** @type {NodeListOf<HTMLFormElement>} */
const destructiveForms = document.querySelectorAll('form[data-confirm]');

destructiveForms.forEach(/** @param {HTMLFormElement} form @returns {void} */ form => {
	form.addEventListener('submit', /** @param {SubmitEvent} event @returns {void} */ event => {
		/** @type {string} */
		const message = form.dataset.confirm ?? '';
		if (!confirmation || typeof confirmation.showModal !== 'function') {
			if (!window.confirm(message)) event.preventDefault();
			return;
		}
		event.preventDefault();
		pendingForm = form;
		/** @type {HTMLElement | null} */
		const text = confirmation.querySelector('#confirmation-text');
		if (text) text.textContent = message;
		confirmation.showModal();
	});
});

confirmation?.addEventListener('close', /** @returns {void} */() => {
	if (!confirmation) return;
	/** @type {HTMLFormElement | null} */
	const form = pendingForm;
	pendingForm = null;
	if (confirmation.returnValue === 'confirm' && form) {
		HTMLFormElement.prototype.submit.call(form);
	}
	confirmation.returnValue = '';
});

/**
 * Parse a positive decimal as an integer at the specified scale.
 * @param {string} raw
 * @param {number} scale
 * @returns {bigint | null}
 */
function parseFixed(raw, scale) {
	const normalized = raw.trim().replace(/[\s\u00a0]/g, '').replace(',', '.');
	const pattern = new RegExp(`^\\d+(?:\\.\\d{1,${scale}})?$`);
	if (!pattern.test(normalized)) return null;
	const [whole = '0', fraction = ''] = normalized.split('.');
	const value = BigInt(whole) * 10n ** BigInt(scale) + BigInt(fraction.padEnd(scale, '0'));
	return value > 0n ? value : null;
}

/** @param {bigint} value @param {number} scale @returns {string} */
function formatFixed(value, scale) {
	const factor = 10n ** BigInt(scale);
	return `${value / factor}.${(value % factor).toString().padStart(scale, '0')}`;
}


/** @param {bigint} numerator @param {bigint} denominator @returns {string} */
function formatRate(numerator, denominator) {
	// Keep small rates visible; retain the exact rational separately for recalculation.
	return formatFixed((numerator * 10n ** 36n + denominator / 2n) / denominator, 36).replace(/\.?0+$/, '');
}

/** @type {HTMLFormElement | null} */
const entryForm = document.querySelector('[data-entry-form]');
if (entryForm) {
	/** @type {HTMLFieldSetElement | null} */
	const fields = entryForm.querySelector('[data-transfer-fields]');
	/** @type {HTMLInputElement | null} */
	const source = entryForm.querySelector('[name="amount"]');
	/** @type {HTMLSelectElement | null} */
	const sourceCurrency = entryForm.querySelector('[name="currency"]');
	/** @type {HTMLInputElement | null} */
	const destination = entryForm.querySelector('[name="destination_amount"]');
	/** @type {HTMLSelectElement | null} */
	const destinationCurrency = entryForm.querySelector('[name="destination_currency"]');
	/** @type {HTMLInputElement | null} */
	const rate = entryForm.querySelector('[data-exchange-rate]');
	/** @type {HTMLOutputElement | null} */
	const preview = entryForm.querySelector('[data-transfer-preview]');
	/** @type {HTMLElement | null} */
	const amountLabel = entryForm.querySelector('[data-source-amount-label]');
	if (fields && source && sourceCurrency && destination && destinationCurrency && rate && preview) {
		/** @type {bigint | null} */
		let numerator = null;
		/** @type {bigint | null} */
		let denominator = null;
		/** @param {EventTarget | null} changed @returns {void} */
		const updateTransfer = changed => {
			/** @type {HTMLInputElement | null} */
			const kind = entryForm.querySelector('[name="kind"]:checked');
			const transfer = kind?.value === 'transfer';
			fields.hidden = !transfer;
			fields.disabled = !transfer;
			destination.required = transfer;
			rate.required = transfer;
			if (amountLabel) amountLabel.textContent = transfer ? 'Сумма списания' : 'Сумма';
			if (!transfer) return;
			const amount = parseFixed(source.value, 2);
			if (changed === rate) {
				rate.dataset.computed = 'false';
				numerator = parseFixed(rate.value, 8);
				denominator = 100000000n;
				if (!numerator) { preview.value = 'Укажите положительный курс, до 8 знаков после запятой.'; return; }
			}
			if (!amount) { preview.value = 'Укажите положительную сумму списания.'; return; }
			if (changed === destination || changed === null || numerator === null) {
				const received = parseFixed(destination.value, 2);
				if (received) {
					numerator = received;
					denominator = amount;
					rate.value = formatRate(numerator, denominator);
					rate.dataset.computed = 'true';
				} else {
					if (changed === destination) { rate.value = ''; numerator = null; }
					preview.value = 'Укажите курс или сумму получения — второе поле вычислится автоматически.';
					return;
				}
			} else if (denominator !== null) {
				const received = (amount * numerator + denominator / 2n) / denominator;
				destination.value = formatFixed(received, 2);
			}
			if (sourceCurrency.value === destinationCurrency.value) {
				preview.value = 'Исходная и получаемая валюты должны различаться.';
				return;
			}
			preview.value = `${source.value} ${sourceCurrency.value} → ${destination.value} ${destinationCurrency.value}. Сохраняются обе суммы; курс вычисляется по ним.`;
		};
		entryForm.addEventListener('input', event => updateTransfer(event.target));
		entryForm.addEventListener('change', event => updateTransfer(event.target));
		updateTransfer(null);
	}
	/** @type {HTMLInputElement | null} */
	const occurredAt = entryForm.querySelector('[name="occurred_at"]');
	/** @type {HTMLElement | null} */
	const plannedNote = entryForm.querySelector('[data-planned-note]');
	const serverEpoch = Date.parse(`${entryForm.dataset.serverNow}Z`);
	const openedAt = performance.now();
	if (occurredAt && plannedNote && Number.isFinite(serverEpoch)) {
		/** @returns {void} */
		const updatePlannedNote = () => {
			const now = new Date(serverEpoch + performance.now() - openedAt).toISOString().slice(0, 19);
			const planned = occurredAt.value > now;
			plannedNote.hidden = !planned;
			plannedNote.textContent = 'Запланированная запись: войдёт в текущий баланс, когда наступят указанные дата и время.';
		};
		occurredAt.addEventListener('input', updatePlannedNote);
		updatePlannedNote();
	}
}

/** @typedef {HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement} ValidatedField */
/** @type {number} */
let errorCounter = 0;

/** @param {ValidatedField} field @returns {string} */
function validationMessage(field) {
	if (field.matches(':disabled')) return '';
	const value = field.value.trim();
	if (field.required && !value) return 'Заполните это поле.';
	if (field.name === 'name' && (!value || value.length > 80)) return 'Название должно содержать от 1 до 80 символов.';
	if (field.name === 'comment' && (!value || value.length > 2000)) return 'Укажите комментарий длиной от 1 до 2000 символов.';
	const capital = ['RUB', 'USD', 'EUR'].includes(field.name);
	if (capital || field.name === 'amount' || field.name === 'destination_amount') {
		const normalized = value.replace(/[\s\u00a0]/g, '').replace(',', '.');
		const pattern = capital ? /^-?\d+(?:\.\d{1,2})?$/ : /^\d+(?:\.\d{1,2})?$/;
		if (!pattern.test(normalized)) return 'Введите сумму с точностью до двух знаков после запятой.';
		const absolute = normalized.replace(/^-/, '');
		const amount = parseFixed(absolute, 2);
		if (!capital && !amount) return 'Сумма должна быть больше нуля.';
		if (amount !== null && amount > 900000000000000000n) return 'Слишком большая сумма.';
	}
	if (field.hasAttribute('data-exchange-rate')) {
		const scale = field.dataset.computed === 'true' ? 36 : 8;
		if (parseFixed(value, scale) === null) return 'Укажите положительный курс, до 8 знаков после запятой.';
	}
	if (field.name === 'destination_currency' && field.form) {
		/** @type {HTMLSelectElement | null} */
		const source = field.form.querySelector('[name="currency"]');
		if (source?.value === field.value) return 'Выберите две разные валюты.';
	}
	if (field.name === 'name' && field.form?.action.includes('/library/categories') && value === 'Без категории') return 'Это название зарезервировано.';
	if (field.name === 'to' || field.name === 'end_date') {
		/** @type {HTMLInputElement | null} */
		const start = field.form?.querySelector(field.name === 'to' ? '[name="from"]' : '[data-period-date]') ?? null;
		if (value && start?.value && start.value > value) return 'Конечная дата должна быть не раньше начальной.';
	}
	if (field.validity.badInput || field.validity.typeMismatch || field.validity.rangeOverflow || field.validity.rangeUnderflow || field.validity.stepMismatch || field.validity.patternMismatch) return 'Проверьте формат и допустимое значение поля.';
	return '';
}

/** @param {ValidatedField} field @param {boolean} show @returns {boolean} */
function validateField(field, show) {
	const message = validationMessage(field);
	field.setCustomValidity(message);
	if (show) {
		field.dataset.touched = 'true';
		field.classList.toggle('field-invalid', Boolean(message));
		if (message) field.setAttribute('aria-invalid', 'true'); else field.removeAttribute('aria-invalid');
		let errorId = field.dataset.errorId;
		if (!errorId) {
			errorId = `field-error-${++errorCounter}`;
			field.dataset.errorId = errorId;
			const error = document.createElement('span');
			error.id = errorId;
			error.className = 'field-error';
			error.setAttribute('aria-live', 'polite');
			field.insertAdjacentElement('afterend', error);
			field.setAttribute('aria-describedby', `${field.getAttribute('aria-describedby') ?? ''} ${errorId}`.trim());
		}
		const error = document.getElementById(errorId);
		if (error) { error.textContent = message; error.hidden = !message; }
	}
	return !message;
}

document.querySelectorAll('form').forEach(form => {
	/** @type {NodeListOf<ValidatedField>} */
	const fields = form.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="submit"]):not([type="button"]), select, textarea');
	if (!fields.length) return;
	form.noValidate = true;
	fields.forEach(field => field.addEventListener('blur', () => validateField(field, true)));
	form.addEventListener('input', () => fields.forEach(field => { if (field.dataset.touched) validateField(field, true); }));
	form.addEventListener('change', () => fields.forEach(field => { if (field.dataset.touched) validateField(field, true); }));
	form.addEventListener('submit', event => {
		const invalid = Array.from(fields).filter(field => !validateField(field, true));
		if (invalid.length) { event.preventDefault(); invalid[0]?.focus(); }
	});
});
