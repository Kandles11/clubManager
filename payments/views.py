from operator import ne

from django.shortcuts import render, get_object_or_404, redirect

from accounts.models import UserStub
from core.models import UserProfile
from .forms import PaymentSignInForm
import configparser
from django.utils import timezone
import json
from square.client import Client
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.models import User
from django.views import View
from payments.models import Payment, Product, PurchasedProduct
from django.db import transaction
from pathlib import Path
from .utilities import can_purchase_product, calculate_cost_with_square_fee
from clubManager import settings
from core.utilities import get_layout_data

if settings.ENABLE_SQUARE_PAYMENTS:
    config_path = Path("config.ini")
    if not config_path.exists():
        raise FileNotFoundError(f"Missing Square config.ini file! Looking in folder: {Path().resolve()}")

    config = configparser.ConfigParser()
    config.read("config.ini")

    CONFIG_TYPE = config.get("DEFAULT", "environment").upper()

    PAYMENT_FORM_URL = (
        "https://web.squarecdn.com/v1/square.js"
        if CONFIG_TYPE == "PRODUCTION"
        else "https://sandbox.web.squarecdn.com/v1/square.js"
    )

    APPLICATION_ID = config.get(CONFIG_TYPE, "square_application_id")
    LOCATION_ID = config.get(CONFIG_TYPE, "square_location_id")
    ACCESS_TOKEN = config.get(CONFIG_TYPE, "square_access_token")

    client = Client(
        access_token=ACCESS_TOKEN,
        environment=config.get("DEFAULT", "environment"),
    )

    location = client.locations.retrieve_location(location_id=LOCATION_ID).body["location"]
    ACCOUNT_CURRENCY = location["currency"]
    ACCOUNT_COUNTRY = location["country"]


class PaymentSuccessView(View):
    template_name = "payment_success.html"

    def get(self, request, payment_id):
        layout_data = get_layout_data(request)
        payment = get_object_or_404(Payment, id=payment_id)

        product: Product = payment.purchased_products.first().product
        user_profile, _ = UserProfile.objects.get_or_create(user=payment.user)

        is_user_missing_discord_account_link = user_profile.discord_id is None
        is_payment_is_for_member_dues = hasattr(product, "term")
        message = (
            f"One last step: go to the {layout_data['settings'].organization_name or 'club'} Discord server and type /link in any channel, so we can give you access to member-only channels!"
            if is_user_missing_discord_account_link and is_payment_is_for_member_dues
            else None
        )

        return render(
            request,
            self.template_name,
            {**layout_data, "product_name": product.name, "payment": payment, "message": message},
        )


class ChooseUserView(View):
    template_name = "choose_user.html"

    def get(self, request, product_id):
        layout_data = get_layout_data(request)
        product = get_object_or_404(Product, id=product_id)
        form = PaymentSignInForm()
        return render(request, self.template_name, {**layout_data, "form": form, "product_name": product.name})

    def post(self, request, product_id):
        layout_data = get_layout_data(request)
        form = PaymentSignInForm(request.POST)
        product = get_object_or_404(Product, id=product_id)
        if form.is_valid():
            username = form.cleaned_data["username"]
            payment_choice = form.cleaned_data["payment_method"]
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                try:
                    UserStub.create(net_id=username, after_registration_redirect_destination=request.get_full_path())
                    message = "Check your UT Dallas email address for an email from us with a link to get registered and finish this payment!"
                except:
                    message = "We couldn't find your user in our system. Please ask an officer for further assistance!"
                return render(
                    request,
                    self.template_name,
                    {
                        **layout_data,
                        "form": form,
                        "message": message,
                        "product_name": product.name,
                    },
                )

            message = can_purchase_product(product, user)
            if message:
                return render(
                    request,
                    self.template_name,
                    {**layout_data, "form": form, "message": message, "product_name": product.name},
                )
            else:
                if payment_choice == "square_api":
                    return redirect("payment_form", product_id=product_id, user_id=user.id)
                elif payment_choice == "cash":
                    with transaction.atomic():
                        payment = Payment(
                            method=Payment.Method.cash,
                            user=user,
                            amount_cents=product.amount_cents,
                        )
                        payment.save()
                        purchased_product = PurchasedProduct(product=product, payment=payment)
                        purchased_product.save()
                    return redirect("payment_success", payment_id=purchased_product.payment.id)
                else:
                    raise Exception("Invalid payment choice")
        return render(request, self.template_name, {**layout_data, "form": form, "product_name": product.name})


def product_payment(request, product_id, user_id):
    if settings.ENABLE_SQUARE_PAYMENTS == False:
        return HttpResponse("Payments are currently disabled.", status=403)

    layout_data = get_layout_data(request)
    product = get_object_or_404(Product, id=product_id)
    fees = calculate_cost_with_square_fee(product.amount_cents)

    process_fee = fees["square_fee_cents"]
    total_cost = fees["total_payment_amount_cents"]

    return render(
        request,
        "product_payment.html",
        {
            **layout_data,
            "product": product,
            "user_id": user_id,
            "process_fee": process_fee,
            "total_cost": total_cost,
            "APPLICATION_ID": APPLICATION_ID,
            "LOCATION_ID": LOCATION_ID,
            "ACCESS_TOKEN": ACCESS_TOKEN,
            "PAYMENT_FORM_URL": PAYMENT_FORM_URL,
            "ACCOUNT_CURRENCY": ACCOUNT_CURRENCY,
            "ACCOUNT_COUNTRY": ACCOUNT_COUNTRY,
        },
    )


@csrf_exempt
def process_square_payment(request, product_id, user_id):
    if request.method == "POST":
        if settings.ENABLE_SQUARE_PAYMENTS == False:
            return JsonResponse({"detail": "Payments are currently disabled."}, safe=False, status=403)
        requestBody = json.loads(request.body)

        token = requestBody.get("token")
        idempotency_key = requestBody.get("idempotencyKey")

        product = get_object_or_404(Product, id=product_id)
        user = get_object_or_404(User, id=user_id)

        message = can_purchase_product(product, user)
        if message:
            return JsonResponse({"square_res": {"errors": [{"detail": message}]}}, safe=False)

        fees = calculate_cost_with_square_fee(product.amount_cents)

        with transaction.atomic():
            payment = Payment(
                method=Payment.Method.square_api,
                user=user,
                amount_cents=fees["total_payment_amount_cents"],
            )
            payment.save()
            purchased_product = PurchasedProduct(product=product, payment=payment)
            purchased_product.save()

        create_payment_response = client.payments.create_payment(
            body={
                "source_id": token,
                "idempotency_key": idempotency_key,
                "amount_money": {
                    "amount": payment.amount_cents,
                    "currency": ACCOUNT_CURRENCY,
                },
                "reference_id": str(payment.pk),
                "note": str(payment),
            }
        )

        payment.metadata = {"square_response_body": create_payment_response.body}
        payment.save()

        if create_payment_response.is_success():
            payment.completed_at = timezone.now()
            payment.save()
            return JsonResponse(
                {
                    "square_res": create_payment_response.body,
                    "payment_success_page": f"/payments/{payment.id}/success",
                },
                safe=False,
            )
        elif create_payment_response.is_error():
            return JsonResponse({"square_res": create_payment_response.body}, safe=False)
    else:
        return JsonResponse({"error": "Invalid request method"}, safe=False, status_code=405)
