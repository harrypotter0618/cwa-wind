import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forecast_app', ROOT / 'wrf3km-api/app.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class TemperatureTests(unittest.TestCase):
    def test_units_and_missing(self):
        self.assertAlmostEqual(a.temperature_celsius(273.15, 'K'), 0)
        self.assertIsNone(a.temperature_celsius(9999, 'K'))
        self.assertIsNone(a.temperature_celsius(float('nan'), 'K'))
        self.assertIsNone(a.temperature_celsius(20, 'unknown'))
        f = {0: {'temperature_c': [20]}, 6: {'temperature_c': [26]}}
        self.assertEqual(a.interpolate_temperature(f, [0, 6], .5, 0), 23)
        self.assertEqual(a.interpolate_temperature(f, [0], None, 0), 20)
        f[6]['temperature_c'][0] = None
        self.assertIsNone(a.interpolate_temperature(f, [0, 6], .5, 0))

    def test_field_selection(self):
        for short, level, expected in [('2t',2,True),('t',2,True),('2d',2,False),('skt',0,False),('t',850,False)]:
            fields={'shortName':short,'level':level,'typeOfLevel':'heightAboveGround'}
            with patch.object(a,'safe_get',side_effect=lambda gid,key,default=None:fields.get(key,default)):
                self.assertEqual(a.is_temperature_message(None),expected)

    def test_route_eta_rests_summary(self):
        init = a.parse_departure('2026-11-10T00:00:00+08:00')
        route = a.parse_gpx((ROOT/'ride-api/routes/current.gpx').read_bytes())
        def pick(samples, latest):
            specs, hours = a._time_specs_for_cycle(samples, latest)
            return dict(selected_init=latest,time_specs=specs,needed_hours=hours,fallback_hours=0)
        def fields(fh, coords, expected, gridmap):
            n=len(coords)
            return dict(matches_latest_cycle=True,u={'values':[1]*n,'points':[[{'distance_km':1}]]*n},v={'values':[1]*n},temperature_c=[20+fh]*n)
        with patch.object(a,'_probe_remote_grib_meta',return_value={'init_epoch':init}), patch.object(a,'select_coherent_published_cycle',side_effect=pick), patch.object(a,'get_grid_mappings',return_value=None), patch.object(a,'load_uv_many_for_cycle',side_effect=fields):
            args=dict(route=route,route_source='test',route_id='test',departure='2026-11-10T00:00:00+08:00',speed_kmh=20,step_km=20,start_km=0,end_km=60)
            base=a._calculate_route_forecast(**args)
            rest=a._calculate_route_forecast(**args,rest_points=[{'km':20,'minutes':60,'name':'test'}])
        self.assertEqual([s['temperature_c'] for s in base['samples']],[20,21,22,23])
        self.assertEqual([s['temperature_c'] for s in rest['samples']],[20,21,23,24])
        self.assertEqual(rest['summary']['max_temperature']['km'],60)
        self.assertEqual(rest['summary']['min_temperature']['temperature_c'],20)
        Path('/tmp/temperature-fixture.json').write_text(json.dumps(rest))

if __name__ == '__main__':
    unittest.main()
