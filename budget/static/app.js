'use strict';

// Switching profiles is a regular, CSRF-protected form submission.
document.querySelectorAll('[data-profile-switch]').forEach(select => {
	select.addEventListener('change', () => select.form.requestSubmit());
	select.form.querySelector('.profile-submit').hidden = true;
});

const periodForm = document.querySelector('[data-period-form]');
if (periodForm) {
	const input = periodForm.querySelector('[data-period-date]');
	function setPeriod() {
		const period = periodForm.querySelector('[name="period"]:checked').value;
		const old = input.value;
		input.type = period === 'day' ? 'date' : period === 'month' ? 'month' : 'number';
		input.removeAttribute('min');
		input.removeAttribute('max');
		if (period === 'year') {
			input.min = '1'; input.max = '9999'; input.step = '1';
			input.value = old.slice(0, 4);
		} else if (period === 'month') {
			input.value = old.length >= 7 ? old.slice(0, 7) : `${old}-01`;
		} else {
			input.value = old.length === 10 ? old : old.length === 7 ? `${old}-01` : `${old}-01-01`;
		}
	}
	periodForm.querySelectorAll('[name="period"]').forEach(radio => radio.addEventListener('change', setPeriod));
	setPeriod();
}

// Advance the maximum time using the server's local clock, independently of
// the phone's time zone. The server performs the authoritative validation.
document.querySelectorAll('[data-now-max]').forEach(input => {
	const initial = Date.parse(`${input.max}Z`);
	const started = performance.now();
	if (Number.isFinite(initial)) {
		setInterval(() => {
			input.max = new Date(initial + performance.now() - started).toISOString().slice(0, 19);
		}, 1000);
	}
});

const confirmation = document.querySelector('#confirmation');
let pendingForm = null;
document.querySelectorAll('form[data-confirm]').forEach(form => {
	form.addEventListener('submit', event => {
		if (!confirmation || typeof confirmation.showModal !== 'function') {
			if (!window.confirm(form.dataset.confirm)) event.preventDefault();
			return;
		}
		event.preventDefault();
		pendingForm = form;
		confirmation.querySelector('#confirmation-text').textContent = form.dataset.confirm;
		confirmation.showModal();
	});
});
confirmation?.addEventListener('close', () => {
	const form = pendingForm;
	pendingForm = null;
	if (confirmation.returnValue === 'confirm' && form) {
		HTMLFormElement.prototype.submit.call(form);
	}
	confirmation.returnValue = '';
});
