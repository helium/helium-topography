import os

import rasterio
import sqlalchemy.exc
from sqlalchemy.sql.expression import func
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session

import pandas as pd
from models.tables import ChallengeReceiptsParsed, TopographyResults
from models.transactions.poc_receipts_v2 import PocReceiptsV2
import warnings

from feature_extraction import *

import h3
from haversine import haversine, Unit
import pickle
import time
from dotenv import load_dotenv


np.seterr(invalid="ignore")

load_dotenv()


TRAINED_SVM_PATH = "static/trained_models/svm/2022-02-06T16_23_54.mdl"
TRAINED_GP_PATH = "static/trained_models/gaussian_process/2022-02-04T16_28_14.mdl"
TRAINED_ISO_PATH = "static/trained_models/isolation_forest/2022-02-04T16_31_09.mdl"
GAMING_DISTRIBUTION_PATH = "static/assets/gaming_results.csv"
NOMINAL_DISTRIBUTION_PATH = "static/assets/nominal_results.csv"


def load_model(path: str):
    print("Loading trained model...")
    with open(path, "rb") as f:
        model = pickle.load(f)
    print("done.")
    return model


svm = load_model(TRAINED_SVM_PATH)
iso_forest = load_model(TRAINED_ISO_PATH)

helium_lite_engine = create_engine(os.getenv("POSTGRES_CONNECTION_STRING"))
helium_lite_session = Session(helium_lite_engine)


def get_current_height(helium_lite_session: Session) -> int:
    return helium_lite_session.query(func.max(ChallengeReceiptsParsed.block)).one()


def map_topo_features(x, dataset):
    try:
        d_vec = np.arange(0, x["distance_m"] / 1000, 0.3)
        index_list = []

        for j in range(len(d_vec)):
            c = inverse_haversine(x["transmitter_coords"], d_vec[j], x["bearing"])
            index_list.append((c[1], c[0]))

        elevation_profile = np.zeros_like(d_vec)

        for i, e in enumerate(rasterio.sample.sample_gen(dataset, index_list, 1)):
            elevation_profile[i] = e
        return extract_topographic_features(d_vec, level_profile(d_vec, elevation_profile))
    except:
        # return nans, but same structure as a valid output to ease processing later
        return {
            "ra": np.nan,
            "rq": np.nan,
            "rp": np.nan,
            "rv": np.nan,
            "rz": np.nan,
            "rsk": np.nan,
            "rku": np.nan,
            "deepest_barrier": np.nan,
            "n_barriers": np.nan
        }


def generate_stats(details_df, gateway_locations, eval_mean, current_height):
    N = 1000
    results = []

    for i, hotspot in enumerate(set(details_df["witness_address"])):

        witness_coords = list(details_df["transmitter_coords"][details_df["witness_address"] == hotspot])
        if len(witness_coords) < 3:
            continue

        asserted_location = gateway_locations.loc[hotspot]["asserted_location"]
        asserted_hex_res8 = gateway_locations.loc[hotspot]["asserted_hex_res8"]
        predicted_locations = []

        for i in range(N):
            idx1, idx2, idx3 = np.random.permutation(len(witness_coords))[:3]
            u = haversine(witness_coords[idx1], witness_coords[idx2], unit=Unit.KILOMETERS)
            if u <= 0.05:
                # pair of witnesses is in the same hex
                continue

            r1 = eval_mean[idx1]
            r2 = eval_mean[idx2]

            if r1 > 50 or r2 > 50:
                # try again
                i -= 1
                continue

            x_prime = (r1 ** 2 - r2 ** 2 + u ** 2) / (2 * u)
            if (r1 ** 2 - x_prime ** 2) < 0:
                # chance that sampled radii are negative, especially when extrapolating gaussian process model
                continue
            y_prime_1 = np.sqrt(r1 ** 2 - x_prime ** 2)
            y_prime_2 = -y_prime_1

            phi_1 = get_bearing(witness_coords[idx1][0], witness_coords[idx1][1], y_prime_1 + witness_coords[idx1][0],
                                x_prime + witness_coords[idx1][1])
            phi_2 = get_bearing(witness_coords[idx1][0], witness_coords[idx1][1], y_prime_2 + witness_coords[idx1][0],
                                x_prime + witness_coords[idx1][1])

            pt_1 = inverse_haversine(witness_coords[idx1], r1, phi_1)
            pt_2 = inverse_haversine(witness_coords[idx1], r1, phi_2)

            # trilateration has 2 possible solutions -> choose the point closest to the asserted location
            if haversine(asserted_location, pt_1) < haversine(asserted_location, pt_2):
                predicted_location = pt_1
            else:
                predicted_location = pt_2

            predicted_locations.append(predicted_location)

        predicted_lat = [c[0] for c in predicted_locations]
        predicted_lon = [c[1] for c in predicted_locations]
        monte_carlo_results = pd.DataFrame([predicted_lat, predicted_lon]).transpose()
        monte_carlo_results.columns = ["lat", "lon"]

        rings = h3.k_ring(asserted_hex_res8, 5)
        n_points = monte_carlo_results.shape[0]
        pts_in_hex = 0
        for i in range(n_points):
            if h3.geo_to_h3(monte_carlo_results.iloc[i].lat, monte_carlo_results.iloc[i].lon, 8) in rings:
                pts_in_hex += 1
        try:
            p = str(np.round(100 * pts_in_hex / n_points, 1))
        except:
            p = "0"


        results.append({"address": hotspot,
                        "percent_predictions_within_5_res8_krings": p,
                        "prediction_error_km": haversine(asserted_location, (np.median(predicted_lat), np.median(predicted_lon)),
                                                         unit=Unit.KILOMETERS),
                        "n_outliers": len(details_df[(details_df["witness_address"] == hotspot) & (details_df["outliers"] < 0)]),
                        "n_beaconers_heard": len(details_df[details_df["witness_address"] == hotspot])})


    results_df = pd.DataFrame(results)
    results_df["block"] = current_height
    results_df.index = results_df["address"]

    result_rows = results_df.to_dict("index")
    return result_rows


def upsert_predictions(result_rows, helium_lite_session: Session):
    # Find all new rows and build mappings
    for each in (
            helium_lite_session.query(TopographyResults.address).filter(TopographyResults.address.in_(result_rows.keys())).all()
    ):
        result_rows.pop(each.address)

    # Bulk mappings for everything that needs to be inserted (no need to update these)
    entries_to_put = [v for v in result_rows.values()]
    helium_lite_session.bulk_insert_mappings(TopographyResults, entries_to_put)
    helium_lite_session.flush()
    try:
        helium_lite_session.commit()
    except sqlalchemy.exc.OperationalError:
        print("Rolling back bulk insert due to operational error")
        helium_lite_session.rollback()


def get_witness_edges(helium_lite_session: Session):
    receipts_parsed = helium_lite_session.query(ChallengeReceiptsParsed.transmitter_address, ChallengeReceiptsParsed.witness_address,
                                                ChallengeReceiptsParsed.witness_signal, ChallengeReceiptsParsed.witness_snr,
                                                ChallengeReceiptsParsed.tx_power).all()

    print("Performing some pandas transforms")
    t = time.time()
    witness_edges = pd.DataFrame(receipts_parsed).groupby(["transmitter_address", "witness_address"]).mean().reset_index()
    witness_edges = witness_edges.merge(gateway_locations, left_on="transmitter_address", right_on="address")
    witness_edges = witness_edges.merge(gateway_locations, left_on="witness_address", right_on="address")
    witness_edges["transmitter_coords"] = witness_edges["location_x"].map(h3.h3_to_geo)
    witness_edges["witness_coords"] = witness_edges["location_y"].map(h3.h3_to_geo)
    witness_edges["distance_m"] = witness_edges.apply(lambda x: haversine(x["transmitter_coords"], x["witness_coords"], Unit.METERS), axis=1)

    witness_edges["bearing"] = witness_edges.apply(lambda x: get_bearing(x["transmitter_coords"][0], x["transmitter_coords"][1],
                                                                         x["witness_coords"][0], x["witness_coords"][1]), axis=1)
    print(f"Done, {time.time() - t} s")
    return witness_edges


def get_witness_edges_for_address(helium_lite_session: Session, address: str, limit: int = 1000):

    witness_edges = pd.read_sql(f"""select transmitter_address, witness_address, witness_signal as rssi, witness_snr as snr, tx_power
        from challenge_receipts_parsed where witness_address = '{address}' and tx_power is not NULL limit {limit};""", con=helium_lite_session.bind)

    if len(witness_edges) > 1:
        witness_edges = witness_edges.groupby(["transmitter_address", "witness_address"]).mean().reset_index()
        witness_edges = witness_edges.rename({"witness_signal": "rssi", "witness_snr": "snr"})
        witness_edges = witness_edges.merge(gateway_locations, left_on="transmitter_address", right_on="address")
        witness_edges = witness_edges.merge(gateway_locations, left_on="witness_address", right_on="address")
        witness_edges["transmitter_coords"] = witness_edges["location_x"].map(h3.h3_to_geo)
        witness_edges["witness_coords"] = witness_edges["location_y"].map(h3.h3_to_geo)
        witness_edges["distance_m"] = witness_edges.apply(lambda x: haversine(x["transmitter_coords"], x["witness_coords"], Unit.METERS), axis=1)

        witness_edges["bearing"] = witness_edges.apply(lambda x: get_bearing(x["transmitter_coords"][0], x["transmitter_coords"][1],
                                                                             x["witness_coords"][0], x["witness_coords"][1]), axis=1)
        return witness_edges
    else:
        return None


with warnings.catch_warnings():
    # RuntimeWarning for calculating mean of empty slice. safe to ignore as it's handled elsewhere
    warnings.simplefilter("ignore", category=RuntimeWarning)
    while True:
        current_height = get_current_height(helium_lite_session)

        print("Getting gateway inventory")
        t = time.time()
        gateway_locations = pd.read_sql("select address, location, gain, elevation from gateway_inventory order by last_block desc;",
                                        con=helium_lite_session.bind)
        print(f"Done, {time.time() - t} s")

        gateway_locations = gateway_locations.set_index("address").dropna()
        gateway_locations["asserted_location"] = gateway_locations["location"].map(h3.h3_to_geo)
        gateway_locations["asserted_hex_res8"] = gateway_locations.apply(lambda x: h3.h3_to_parent(x["location"], 8), axis=1)

        n_gateways = len(gateway_locations)
        t = time.time()
        for i, address in enumerate(gateway_locations.index):
            if i % 1000 == 0:
                print(f"{i} / {n_gateways} gateways, dt: {time.time() - t} s")
                t = time.time()

            try:
                witness_edges = get_witness_edges_for_address(helium_lite_session, address)
            except (sqlalchemy.exc.NoResultFound, KeyError, ValueError):
                continue
            if witness_edges is None:
                continue

            n_edges = len(witness_edges)
            if n_edges < 2:
                continue
            path_features, path_details = [], []
            
            witness_edges = witness_edges[(witness_edges["distance_m"] > 50) & (witness_edges["distance_m"] < 50e3)]

            with rasterio.open(os.getenv("VRT_PATH")) as dataset:
                # tried .apply, iterrows(), to_dict -> iterate. this is fastest by a slight margin (~50s / 1000 rows)
                for i, x in witness_edges.iterrows():
                    if x["distance_m"] > 50e3 or x["distance_m"] < 50:
                        continue

                    features = map_topo_features(x, dataset)
                    if np.isnan(features["ra"]):
                        continue

                    try:
                        features["tx_power"] = x["tx_power"]
                        features["gain_beacon"] = x["gain_x"]
                        features["gain_witness"] = x["gain_y"]
                        features["rssi"] = x["rssi"]
                        features["snr"] = x["snr"]
                        features["distance_m"] = x["distance_m"]

                        details = {"transmitter_address": x["transmitter_address"],
                                   "witness_address": x["witness_address"],
                                   "transmitter_coords": x["transmitter_coords"]}

                        path_features.append(features)
                        path_details.append(details)
                    except KeyError:
                        continue


            if len(path_details) < 1 or len(path_features) < 1:
                # didn't find any valid edges
                continue

            features_df = pd.DataFrame(path_features)
            details_df = pd.DataFrame(path_details)

            X_eval = np.array(features_df.drop(["distance_m"], axis=1))

            eval_mean = svm.predict(X_eval)

            outliers = iso_forest.predict(features_df)
            details_df["outliers"] = outliers

            try:
                result_rows = generate_stats(details_df, gateway_locations, eval_mean, current_height)
            except (KeyError, AttributeError):
                continue
            upsert_predictions(result_rows, helium_lite_session)

            path_features, path_details = [], []

