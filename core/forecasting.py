"""Monthly demand forecasting with time-aware model comparison."""

from datetime import date, timedelta
from decimal import Decimal
from math import cos, pi, sin, sqrt

from django.db.models import Max, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from sklearn.ensemble import RandomForestRegressor

from .models import HistoricalSale, Product, Sale, SaleItem, SalesForecast


MIN_HISTORY_MONTHS = 12
FORECAST_HORIZON = 12


def _next_month(month):
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


def _month_range(start, end):
    months = []
    current = start
    while current <= end:
        months.append(current)
        current = _next_month(current)
    return months


def _features(values, month):
    recent = [float(value) for value in values[-3:]]
    while len(recent) < 3:
        recent.insert(0, 0.0)
    month_angle = (month.month - 1) * 2 * pi / 12
    return recent + [sum(recent) / 3.0, sin(month_angle), cos(month_angle)]


def _metrics(actual, predicted):
    errors = [prediction - value for value, prediction in zip(actual, predicted)]
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = sqrt(sum(error * error for error in errors) / len(errors))
    nonzero = [(value, prediction) for value, prediction in zip(actual, predicted) if value != 0]
    mape = (
        sum(abs(prediction - value) / abs(value) for value, prediction in nonzero) / len(nonzero) * 100
        if nonzero
        else None
    )
    return {"MAE": round(mae, 3), "RMSE": round(rmse, 3), "MAPE": round(mape, 2) if mape is not None else None}


def _holt_forecast(values, horizon, alpha=0.45, beta=0.2):
    """Forecast with Holt's level-and-trend exponential smoothing."""
    if not values or horizon <= 0:
        return []
    level = float(values[0])
    trend = float(values[1] - values[0]) if len(values) > 1 else 0.0
    for observed in values[1:]:
        previous_level = level
        level = alpha * float(observed) + (1 - alpha) * (level + trend)
        trend = beta * (level - previous_level) + (1 - beta) * trend
    return [max(0.0, level + step * trend) for step in range(1, horizon + 1)]


def _month_key(value):
    if hasattr(value, "date"):
        value = value.date()
    return value.replace(day=1)


def actual_sales_by_month(product_ids, before_date):
    """Combine imported history with live sales after its last covered date."""
    result = {}
    if not product_ids:
        return result
    imported_rows = (
        HistoricalSale.objects.filter(product_id__in=product_ids, sold_on__lt=before_date, batch__is_simulated=False)
        .annotate(month=TruncMonth("sold_on"))
        .values("product_id", "month")
        .annotate(quantity=Sum("quantity"))
    )
    for row in imported_rows:
        result[(row["product_id"], _month_key(row["month"]))] = row["quantity"]

    sales = SaleItem.objects.filter(
        product_id__in=product_ids,
        sale__status__in=(Sale.Status.OPEN, Sale.Status.PARTIAL, Sale.Status.PAID),
        sale__sold_at__date__lt=before_date,
    )
    product_cutoffs = dict(
        HistoricalSale.objects.filter(product_id__in=product_ids, batch__is_simulated=False)
        .values("product_id")
        .annotate(last_date=Max("sold_on"))
        .values_list("product_id", "last_date")
    )
    live_history_filter = Q()
    for product_id in product_ids:
        cutoff = product_cutoffs.get(product_id)
        if cutoff:
            live_history_filter |= Q(product_id=product_id, sale__sold_at__date__gt=cutoff)
        else:
            live_history_filter |= Q(product_id=product_id)
    sales = sales.filter(live_history_filter)
    sold_rows = (
        sales.annotate(month=TruncMonth("sale__sold_at"))
        .values("product_id", "month")
        .annotate(quantity=Sum("quantity"))
    )
    for row in sold_rows:
        key = (row["product_id"], _month_key(row["month"]))
        result[key] = result.get(key, Decimal("0")) + row["quantity"]
    return result


def _fit_and_predict(history, horizon=FORECAST_HORIZON, forecast_start=None):
    months = _month_range(history[0][0], history[-1][0])
    observed = {month: quantity for month, quantity in history}
    values = [float(observed.get(month, 0)) for month in months]
    if len(values) < MIN_HISTORY_MONTHS:
        return None

    # Walk-forward validation: compare forecasts from several dates without
    # allowing each model to see months that would have been in the future.
    first_origin = max(6, len(values) - FORECAST_HORIZON - 2)
    validation = {
        "Media móvil de 3 meses": {"actual": [], "predicted": []},
        "Suavizado exponencial de Holt": {"actual": [], "predicted": []},
        "Random Forest": {"actual": [], "predicted": []},
    }
    validation_windows = 0
    for origin in range(first_origin, len(values) - 1):
        train_values = values[:origin]
        actual = values[origin:min(origin + FORECAST_HORIZON, len(values))]
        if not actual:
            continue

        moving_values = list(train_values)
        moving_predictions = []
        for _ in actual:
            prediction = max(0.0, sum(moving_values[-3:]) / min(3, len(moving_values)))
            moving_predictions.append(prediction)
            moving_values.append(prediction)

        holt_predictions = _holt_forecast(train_values, len(actual))

        train_x, train_y = [], []
        for index in range(3, origin):
            train_x.append(_features(train_values[:index], months[index]))
            train_y.append(train_values[index])
        if len(train_y) < 3:
            continue
        model = RandomForestRegressor(
            n_estimators=200,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=1,
        )
        model.fit(train_x, train_y)
        model_values = list(train_values)
        model_predictions = []
        for index in range(origin, origin + len(actual)):
            prediction = max(0.0, float(model.predict([_features(model_values, months[index])])[0]))
            model_predictions.append(prediction)
            model_values.append(prediction)

        for name, predictions in (
            ("Media móvil de 3 meses", moving_predictions),
            ("Suavizado exponencial de Holt", holt_predictions),
            ("Random Forest", model_predictions),
        ):
            validation[name]["actual"].extend(actual)
            validation[name]["predicted"].extend(predictions)
        validation_windows += 1

    if not validation_windows:
        return None
    scores = {
        name: _metrics(result["actual"], result["predicted"])
        for name, result in validation.items()
    }
    selected_name, selected_score = min(scores.items(), key=lambda candidate: candidate[1]["MAE"])

    future_values = list(values)
    forecast = []
    current_month = _next_month(months[-1])
    final_model = None
    if selected_name == "Random Forest":
        full_x, full_y = [], []
        for index in range(3, len(values)):
            full_x.append(_features(values[:index], months[index]))
            full_y.append(values[index])
        final_model = RandomForestRegressor(
            n_estimators=200,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=1,
        )
        final_model.fit(full_x, full_y)
    current_month = _next_month(months[-1])
    forecast_start = forecast_start or current_month
    holt_step_count = 0
    step_month = current_month
    while step_month < forecast_start:
        holt_step_count += 1
        step_month = _next_month(step_month)
    holt_future = _holt_forecast(values, holt_step_count + horizon) if selected_name == "Suavizado exponencial de Holt" else []
    holt_index = 0

    def predict_next(target_month):
        nonlocal holt_index
        if selected_name == "Random Forest":
            prediction = float(final_model.predict([_features(future_values, target_month)])[0])
        elif selected_name == "Suavizado exponencial de Holt":
            prediction = holt_future[holt_index]
            holt_index += 1
        else:
            prediction = sum(future_values[-3:]) / min(3, len(future_values))
        prediction = round(max(0.0, prediction), 3)
        future_values.append(prediction)
        return prediction

    while current_month < forecast_start:
        predict_next(current_month)
        current_month = _next_month(current_month)
    for _ in range(horizon):
        prediction = predict_next(current_month)
        forecast.append((current_month, Decimal(str(prediction))))
        current_month = _next_month(current_month)
    selected_score["validation_windows"] = validation_windows
    selected_score["validation_predictions"] = len(validation[selected_name]["actual"])
    selected_score["history_months"] = len(values)
    return selected_name, selected_score, forecast


def generate_monthly_forecasts():
    """Generate the next twelve monthly forecasts for products with a full year of history."""
    today = timezone.localdate()
    current_month = today.replace(day=1)
    last_complete_month = (current_month - timedelta(days=1)).replace(day=1)
    month_is_closing_today = today == _next_month(current_month) - timedelta(days=1)
    history_end_month = current_month if month_is_closing_today else last_complete_month
    history_cutoff = _next_month(current_month) if month_is_closing_today else current_month
    first_future_month = _next_month(current_month)
    generated = 0
    skipped = []
    products = Product.objects.filter(kind=Product.Kind.FINISHED, active=True).order_by("name")
    for product in products:
        simulated_history = {}
        real_history = {}
        imported_rows = (
            HistoricalSale.objects.filter(product=product, sold_on__lt=history_cutoff, batch__is_simulated=True)
            .annotate(month=TruncMonth("sold_on"))
            .values("month")
            .annotate(quantity=Sum("quantity"))
        )
        for row in imported_rows:
            simulated_history[_month_key(row["month"])] = row["quantity"]
        imported_rows = (
            HistoricalSale.objects.filter(product=product, sold_on__lt=history_cutoff, batch__is_simulated=False)
            .annotate(month=TruncMonth("sold_on"))
            .values("month")
            .annotate(quantity=Sum("quantity"))
        )
        for row in imported_rows:
            real_history[_month_key(row["month"])] = row["quantity"]
        combined = {**simulated_history, **real_history}
        sales = SaleItem.objects.filter(
            product=product,
            sale__status__in=(Sale.Status.OPEN, Sale.Status.PARTIAL, Sale.Status.PAID),
            sale__sold_at__date__lt=history_cutoff,
        )
        product_cutoff = HistoricalSale.objects.filter(product=product, batch__is_simulated=False).aggregate(last_date=Max("sold_on"))["last_date"]
        if product_cutoff:
            sales = sales.filter(sale__sold_at__date__gt=product_cutoff)
        sold_rows = sales.annotate(month=TruncMonth("sale__sold_at")).values("month").annotate(quantity=Sum("quantity"))
        for row in sold_rows:
            key = _month_key(row["month"])
            combined[key] = combined.get(key, Decimal("0")) + row["quantity"]
        history = sorted(combined.items())
        if not history:
            skipped.append(product.name)
            continue

        months = _month_range(history[0][0], history_end_month)
        quantities = {month: quantity for month, quantity in history}
        complete_history = [(month, quantities.get(month, Decimal("0"))) for month in months]
        result = _fit_and_predict(complete_history, forecast_start=first_future_month)
        if result is None:
            skipped.append(product.name)
            continue

        model_name, metrics, forecasts = result
        simulated_months = set(
            HistoricalSale.objects.filter(product=product, batch__is_simulated=True, sold_on__lt=history_cutoff)
            .annotate(month=TruncMonth("sold_on")).values_list("month", flat=True)
        )
        real_months = set(
            HistoricalSale.objects.filter(product=product, batch__is_simulated=False, sold_on__lt=history_cutoff)
            .annotate(month=TruncMonth("sold_on")).values_list("month", flat=True)
        )
        metrics["simulated_history_months"] = len(simulated_months - real_months)
        SalesForecast.objects.filter(product=product, period__gte=first_future_month).delete()
        SalesForecast.objects.bulk_create([
            SalesForecast(
                product=product,
                period=period,
                predicted_quantity=quantity,
                model_name=model_name,
                error_metrics=metrics,
            )
            for period, quantity in forecasts
        ])
        generated += len(forecasts)

    return {"created": generated, "skipped": skipped}
