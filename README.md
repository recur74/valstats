# valstats

*Doesn't work if two-factor authentication is turned on*

Plots a users Valorant Deathmatches, showing k/d changes over time.
Plots a users Valorant Competitive games, comparing the players Ranked Rating with an estimated MMR.

```pip install requests matplotlib click frozendict numpy```

```
$ ./valstats.py --help
Usage: valstats.py [OPTIONS] USERNAME PASSWORD

Options:
  --zone TEXT                     Valorant zone (eu, na etc)
  --plot / --no-plot              Plot the result
  --print / --no-print            Print the games to terminal
  --db-name TEXT                  Database name and path. Default is
                                  ./{username}.db
  --weapon [Vandal|Phantom|Sheriff|Bulldog|Guardian|Marshal|Operator|Classic|Ghost]
                                  Show dm stats for this weapon only
  --help                          Show this message and exit.
```
![plot](https://user-images.githubusercontent.com/36073835/133110518-55bcd05b-28e4-4118-a248-c5fcd2e78c96.png)
![rank](https://user-images.githubusercontent.com/36073835/133110547-b9913a40-f3f4-4f55-9fc5-1247fd8dec9c.png)
MMR is calculated as the average rank of all other players in the match.
