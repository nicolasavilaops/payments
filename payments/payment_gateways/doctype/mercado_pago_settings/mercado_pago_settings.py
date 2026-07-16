# Copyright (c) 2026, AvilaOps and contributors
# License: MIT. See LICENSE

"""
# Integrating Mercado Pago

### Validate Currency

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Mercado Pago")
	controller().validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "NuranVerkleij@example.com",
		"payer_name": "Nuran Verkleij",
		"order_id": "111",
		"currency": "BRL",
		"payment_gateway": "Mercado Pago",
	}

	# Redirect the user to this url
	url = controller().get_payment_url(**payment_details)


### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype

Example:

	def on_payment_authorized(payment_status):
		# this method will be called when payment is complete


##### Notes:

payment_status - payment gateway will put payment status on callback.
For Mercado Pago the statuses are: approved, pending, in_process, rejected, cancelled, refunded, charged_back
"""

import json

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway

API_BASE = "https://api.mercadopago.com"


class MercadoPagoSettings(Document):
	supported_currencies = ("BRL",)

	def validate(self):
		create_payment_gateway("Mercado Pago")
		call_hook_method("payment_gateway_enabled", gateway="Mercado Pago")
		if not self.flags.ignore_mandatory:
			self.validate_mercado_pago_credentials()

	def validate_mercado_pago_credentials(self):
		if self.public_key and self.access_token:
			try:
				make_get_request(
					url=f"{API_BASE}/users/me",
					headers={"Authorization": f"Bearer {self.get_password(fieldname='access_token', raise_exception=False)}"},
				)
			except Exception:
				frappe.throw(_("Seems Public Key or Access Token is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Mercado Pago does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		"""Create a Mercado Pago "preference" (Checkout Pro) and return its hosted init_point URL."""
		integration_request = create_request_log(kwargs, service_name="Mercado Pago")

		preference = {
			"items": [
				{
					"title": kwargs.get("title") or kwargs.get("description") or _("Payment"),
					"quantity": 1,
					"currency_id": kwargs.get("currency", "BRL"),
					"unit_price": float(kwargs.get("amount")),
				}
			],
			"payer": {
				"email": kwargs.get("payer_email"),
				"name": kwargs.get("payer_name"),
			},
			"external_reference": integration_request.name,
			"back_urls": {
				"success": get_url(
					f"./api/method/payments.payment_gateways.doctype.mercado_pago_settings.mercado_pago_settings.payment_success?token={integration_request.name}"
				),
				"pending": get_url(f"./payment-success?doctype={kwargs.get('reference_doctype')}&docname={kwargs.get('reference_docname')}"),
				"failure": get_url("./payment-failed"),
			},
			"auto_return": "approved",
			"notification_url": get_url(
				"./api/method/payments.payment_gateways.doctype.mercado_pago_settings.mercado_pago_settings.mercado_pago_webhook"
			),
		}

		try:
			resp = make_post_request(
				f"{API_BASE}/checkout/preferences",
				headers={
					"Authorization": f"Bearer {self.get_password(fieldname='access_token', raise_exception=False)}",
					"Content-Type": "application/json",
				},
				data=json.dumps(preference),
			)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Mercado Pago Preference Creation Failed")
			frappe.throw(_("Could not create Mercado Pago preference"))

		integration_request.db_set("output", json.dumps(resp))
		return resp.get("init_point")

	def get_settings(self):
		return frappe._dict(
			{
				"public_key": self.public_key,
				"access_token": self.get_password(fieldname="access_token", raise_exception=False),
			}
		)

	@frappe.whitelist()
	def clear(self):
		self.public_key = self.access_token = None
		self.redirect_to = None
		self.flags.ignore_mandatory = True
		self.save()


@frappe.whitelist(allow_guest=True)
def mercado_pago_webhook():
	"""Receives payment notifications from Mercado Pago.

	MP calls this with either:
	  - query params: ?type=payment&data.id=<payment_id>  (current format)
	  - legacy query params: ?topic=payment&id=<payment_id>
	"""
	try:
		args = frappe.local.form_dict
		payment_id = args.get("data.id") or args.get("id")
		event_type = args.get("type") or args.get("topic")

		if event_type != "payment" or not payment_id:
			return {"status": "ignored"}

		settings = frappe.get_doc("Mercado Pago Settings")
		payment = make_get_request(
			f"{API_BASE}/v1/payments/{payment_id}",
			headers={"Authorization": f"Bearer {settings.get_password(fieldname='access_token', raise_exception=False)}"},
		)

		integration_request_name = payment.get("external_reference")
		if not integration_request_name or not frappe.db.exists("Integration Request", integration_request_name):
			frappe.log_error(json.dumps(payment), "Mercado Pago webhook: unknown external_reference")
			return {"status": "ignored"}

		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		mp_status = payment.get("status")  # approved, pending, in_process, rejected, cancelled, refunded, charged_back

		status_map = {
			"approved": "Completed",
			"pending": "Queued",
			"in_process": "Queued",
			"rejected": "Failed",
			"cancelled": "Cancelled",
			"refunded": "Cancelled",
			"charged_back": "Cancelled",
		}
		new_status = status_map.get(mp_status, "Queued")
		integration_request.update_status(payment, new_status)

		if new_status == "Completed":
			data = json.loads(integration_request.data)
			ref_doctype = data.get("reference_doctype")
			ref_docname = data.get("reference_docname")
			if ref_doctype and ref_docname:
				try:
					frappe.get_doc(ref_doctype, ref_docname).run_method("on_payment_authorized", "Completed")
				except Exception:
					frappe.log_error(frappe.get_traceback(), "Mercado Pago on_payment_authorized failed")

		frappe.db.commit()
		return {"status": "ok"}

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Mercado Pago webhook failed")
		return {"status": "error"}


@frappe.whitelist(allow_guest=True)
def payment_success():
	"""Landing page hit when the payer is redirected back from Mercado Pago (success/auto_return)."""
	token = frappe.form_dict.get("token")
	if token and frappe.db.exists("Integration Request", token):
		integration_request = frappe.get_doc("Integration Request", token)
		data = json.loads(integration_request.data)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = (
			f"/payment-success?doctype={data.get('reference_doctype')}&docname={data.get('reference_docname')}"
		)
	else:
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = "/payment-failed"
