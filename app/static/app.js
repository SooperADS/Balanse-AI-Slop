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
			/** @type {string} */
			const old = input.value;
			input.type = period === 'day' ? 'date' : period === 'month' ? 'month' : 'number';
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
