from config import DASHBOARD


def plan_next_n_recipies(current_n_recipes: int, delta: int = 3) -> int:
    deltas = DASHBOARD.get_restaurant_delta()

    if deltas.get("delta").get("balance") > 0 and deltas.get("delta").get("reputation") > 0:
        return current_n_recipes + delta
    elif deltas.get("delta").get("balance") < 0 and deltas.get("delta").get("reputation") < 0:
        return max(1, current_n_recipes - delta)
    else:
        return current_n_recipes
