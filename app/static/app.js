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

// Advance the maximum time using the server's local clock, independently of
// the phone's time zone. The server performs the authoritative validation.
/** @type {NodeListOf<HTMLInputElement>} */
const dateInputs = document.querySelectorAll('[data-now-max]');
dateInputs.forEach(/** @param {HTMLInputElement} input @returns {void} */ input => {
	/** @type {number} */
	const initial = Date.parse(`${input.max}Z`);
	/** @type {number} */
	const started = performance.now();
	if (Number.isFinite(initial)) {
		setInterval(/** @returns {void} */() => {
			input.max = new Date(initial + performance.now() - started).toISOString().slice(0, 19);
		}, 1000);
	}
});

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
	const rate = entryForm.querySelector('[name="exchange_rate"]');
	/** @type {HTMLElement | null} */
	const rateField = entryForm.querySelector('[data-rate-field]');
	/** @type {HTMLOutputElement | null} */
	const preview = entryForm.querySelector('[data-transfer-preview]');
	/** @type {HTMLElement | null} */
	const amountLabel = entryForm.querySelector('[data-source-amount-label]');
	if (fields && source && sourceCurrency && destination && destinationCurrency && rate && rateField && preview) {
		/** @returns {void} */
		const updateTransfer = () => {
			/** @type {HTMLInputElement | null} */
			const kind = entryForm.querySelector('[name="kind"]:checked');
			/** @type {HTMLInputElement | null} */
			const mode = entryForm.querySelector('[name="transfer_mode"]:checked');
			const transfer = kind?.value === 'transfer';
			const byRate = mode?.value === 'rate';
			fields.hidden = !transfer;
			fields.disabled = !transfer;
			if (amountLabel) amountLabel.textContent = transfer ? 'Сумма списания' : 'Сумма';
			destination.required = transfer && !byRate;
			destination.readOnly = byRate;
			rateField.hidden = !byRate;
			rate.disabled = !transfer || !byRate;
			rate.required = transfer && byRate;
			destinationCurrency.setCustomValidity('');
			if (!transfer) return;
			if (sourceCurrency.value === destinationCurrency.value) {
				destinationCurrency.setCustomValidity('Выберите две разные валюты');
				preview.value = 'Исходная и получаемая валюты должны различаться.';
				return;
			}
			const amount = parseFixed(source.value, 2);
			if (!amount) {
				preview.value = 'Укажите положительную сумму списания.';
				if (byRate) destination.value = '';
				return;
			}
			if (byRate) {
				const scaledRate = parseFixed(rate.value, 8);
				if (!scaledRate) {
					destination.value = '';
					preview.value = 'Укажите положительный курс, до 8 знаков после запятой.';
					return;
				}
				const received = (amount * scaledRate + 50000000n) / 100000000n;
				if (received <= 0n || received > 900000000000000000n) {
					destination.value = '';
					preview.value = 'Сумма получения должна быть не меньше копейки/цента и не превышать допустимый размер.';
					return;
				}
				destination.value = formatFixed(received, 2);
				preview.value = `${formatFixed(amount, 2).replace('.', ',')} ${sourceCurrency.value} → ${formatFixed(received, 2).replace('.', ',')} ${destinationCurrency.value}. Курс: 1 ${sourceCurrency.value} = ${rate.value} ${destinationCurrency.value}.`;
			} else {
				const received = parseFixed(destination.value, 2);
				if (!received) {
					preview.value = 'Укажите положительную сумму получения — курс будет вычислен автоматически.';
					return;
				}
				const scaledRate = (received * 100000000n + amount / 2n) / amount;
				const rateText = scaledRate === 0n ? 'менее 0,00000001' : formatFixed(scaledRate, 8).replace(/\.?0+$/, '').replace('.', ',');
				preview.value = `Расчётный курс: 1 ${sourceCurrency.value} ≈ ${rateText} ${destinationCurrency.value}. Сохранятся обе указанные суммы.`;
			}
		};
		entryForm.addEventListener('input', updateTransfer);
		entryForm.addEventListener('change', updateTransfer);
		updateTransfer();
	}
}
