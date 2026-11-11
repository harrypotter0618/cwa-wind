"""Rain periods must be coherent; missing rain must not become zero."""
import copy
import unittest
from unittest.mock import patch
from test_temperature import a, ROOT

class PrecipitationTests(unittest.TestCase):
    init = a.parse_departure('2026-11-11T14:00:00+08:00')

    def field(self, start, end, values, init=None):
        return {'precipitation': dict(start_hour=start, end_hour=end,
            values_mm=values, init=self.init if init is None else init)}

    def test_period_boundaries(self):
        for hour, expected in [(0,(0,6)),(6,(0,6)),(6.01,(6,12)),(84,(78,84)),(85,None),(-1,None)]:
            self.assertEqual(a.precipitation_window(hour), expected)

    def test_native_and_cumulative(self):
        fields = {6:self.field(0,6,[2,0,None]),12:self.field(0,12,[5,0,6])}
        self.assertEqual(a.precipitation_at(fields,7,0,self.init)['precipitation_mm'],3)
        self.assertEqual(a.precipitation_at(fields,6,0,self.init)['precipitation_mm'],2)
        self.assertEqual(a.precipitation_at(fields,7,1,self.init)['precipitation_mm'],0)
        self.assertIsNone(a.precipitation_at(fields,7,2,self.init)['precipitation_mm'])
        fields[12]=self.field(6,12,[4])
        self.assertEqual(a.precipitation_at(fields,7,0,self.init)['precipitation_mm'],4)

    def test_mismatch_missing_reset(self):
        baseline={6:self.field(0,6,[2]),12:self.field(0,12,[5])}
        for change in ['cycle','reset','interval','missing','nan']:
            f=copy.deepcopy(baseline)
            if change=='cycle': f[6]['precipitation']['init']-=21600
            if change=='reset': f[12]['precipitation']['values_mm']=[1]
            if change=='interval': f[6]['precipitation']['start_hour']=3
            if change=='missing': del f[6]
            if change=='nan': f[12]['precipitation']['values_mm']=[float('nan')]
            self.assertIsNone(a.precipitation_at(f,7,0,self.init)['precipitation_mm'],change)

    def test_standard_parameter_with_unknown_name(self):
        fields=dict(discipline=0,parameterCategory=1,parameterNumber=8,typeOfLevel='surface',stepType='accum',shortName='unknown')
        with patch.object(a,'safe_get',side_effect=lambda gid,key,default=None:fields.get(key,default)):
            self.assertTrue(a.is_precipitation_message(None))
            fields['parameterNumber']=7
            self.assertFalse(a.is_precipitation_message(None))

    def test_optional_rain_hour_failure_preserves_wind(self):
        route=a.parse_gpx((ROOT/'ride-api/routes/current.gpx').read_bytes())
        def pick(samples,latest):
            specs,hours=a._time_specs_for_cycle(samples,latest)
            return dict(selected_init=latest,time_specs=specs,needed_hours=hours,fallback_hours=0)
        def fields(fh,coords,expected,gridmap):
            n=len(coords)
            return dict(matches_latest_cycle=True,u={'values':[1]*n,'points':[[{'distance_km':1}]]*n},v={'values':[1]*n},temperature_c=[20]*n,**self.field(0,fh,[fh]*n))
        with patch.object(a,'_probe_remote_grib_meta',return_value={'init_epoch':self.init}),patch.object(a,'select_coherent_published_cycle',side_effect=pick),patch.object(a,'get_grid_mappings',return_value=None),patch.object(a,'load_uv_many_for_cycle',side_effect=fields),patch.object(a,'build_uv10_cache',side_effect=RuntimeError('not published')) as build:
            result=a._calculate_route_forecast(route=route,route_source='test',route_id='test',departure='2026-11-12T02:00:00+08:00',speed_kmh=20,step_km=20,start_km=0,end_km=20)
        build.assert_called_once_with(6)
        self.assertTrue(all(x['available'] for x in result['samples']))
        self.assertIsNone(result['samples'][0]['precipitation_mm'])
        self.assertEqual(result['samples'][1]['precipitation_mm'],6)

    def test_exact_rest_and_optional_missing(self):
        route=a.parse_gpx((ROOT/'ride-api/routes/current.gpx').read_bytes())
        def pick(samples,latest):
            specs,hours=a._time_specs_for_cycle(samples,latest)
            return dict(selected_init=latest,time_specs=specs,needed_hours=hours,fallback_hours=0)
        def fields(fh,coords,expected,gridmap):
            n=len(coords)
            return dict(matches_latest_cycle=True,u={'values':[1]*n,'points':[[{'distance_km':1}]]*n},v={'values':[1]*n},temperature_c=[20]*n,**self.field(0,fh,[fh]*n))
        with patch.object(a,'_probe_remote_grib_meta',return_value={'init_epoch':self.init}),patch.object(a,'select_coherent_published_cycle',side_effect=pick),patch.object(a,'get_grid_mappings',return_value=None),patch.object(a,'load_uv_many_for_cycle',side_effect=fields),patch.object(a,'build_uv10_cache',side_effect=RuntimeError('not published')):
            result=a._calculate_route_forecast(route=route,route_source='test',route_id='test',departure='2026-11-11T20:00:00+08:00',speed_kmh=20,step_km=20,start_km=0,end_km=60,rest_points=[dict(km=23,minutes=60,name='rest')])
        self.assertEqual([x['km'] for x in result['samples']],[0,20,23,40,60])
        rest=result['ride']['rest_points'][0]
        self.assertEqual(rest['arrival_taipei'],'2026-11-11T21:09+08:00')
        self.assertEqual(rest['precipitation_mm'],6)
        self.assertTrue(all(x['available'] for x in result['samples']))
        self.assertEqual(result['samples'][0]['precipitation_mm'],6)

if __name__=='__main__': unittest.main()
