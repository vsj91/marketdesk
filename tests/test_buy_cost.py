import unittest
from marketdesk.execution import automatic_buy_cost_check,charges

class BuyCostTests(unittest.TestCase):
 def test_limit_boundary_and_delivery_exit_cost(self):
  value=20000;exit_fee=charges(value,'SELL',True)['total'];buy_fee=100-exit_fee
  result=automatic_buy_cost_check({'max_round_trip_fee_pct':.5},value,buy_fee)
  self.assertEqual(result['estimated_round_trip_fee_pct'],.5)
  self.assertEqual(result['estimated_sell_fee'],exit_fee)
  with self.assertRaisesRegex(ValueError,'Buy too small'):
   automatic_buy_cost_check({'max_round_trip_fee_pct':.5},value,buy_fee+.01)
 def test_absent_setting_preserves_existing_behavior_invalid_setting_blocks(self):
  self.assertIsNone(automatic_buy_cost_check({},100,24))
  for value in (0,-1,True,float('inf'),'bad'):
   with self.subTest(value=value),self.assertRaisesRegex(ValueError,'Invalid'):
    automatic_buy_cost_check({'max_round_trip_fee_pct':value},30000,60)
 def test_fee_model_blocks_small_values_and_accepts_economic_value(self):
  for value in (700,1300,5000,10000,20000,25000):
   with self.subTest(value=value),self.assertRaisesRegex(ValueError,'Buy too small'):
    automatic_buy_cost_check({'max_round_trip_fee_pct':.5},value,charges(value,'BUY')['total'])
  self.assertIsNotNone(automatic_buy_cost_check({'max_round_trip_fee_pct':.5},26000,charges(26000,'BUY')['total']))

if __name__=='__main__':unittest.main()
